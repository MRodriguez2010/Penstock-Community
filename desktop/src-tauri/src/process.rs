use std::{
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};

#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(windows)]
use std::os::windows::{
    io::{AsRawHandle, FromRawHandle, OwnedHandle},
    process::CommandExt,
};

pub struct ManagedChild {
    child: Child,
    stdin: Option<ChildStdin>,
    messages: mpsc::Receiver<String>,
    closed: Arc<AtomicBool>,
    #[cfg(windows)]
    job: OwnedHandle,
}

impl ManagedChild {
    pub fn spawn(mut command: Command) -> Result<Self, String> {
        command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
        #[cfg(unix)]
        command.process_group(0);
        #[cfg(windows)]
        command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);

        let mut child = command.spawn().map_err(|err| format!("Could not start the bundled process: {err}"))?;
        #[cfg(windows)]
        let job = match contain(&child) {
            Ok(job) => job,
            Err(err) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(err);
            }
        };

        let stdin = child.stdin.take();
        let stdout = child.stdout.take().expect("stdout was piped");
        let closed = Arc::new(AtomicBool::new(false));
        let finished = closed.clone();
        let (sender, messages) = mpsc::sync_channel(32);
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if sender.send(line).is_err() {
                    break;
                }
            }
            finished.store(true, Ordering::Release);
        });
        Ok(Self { child, stdin, messages, closed, #[cfg(windows)] job })
    }

    pub fn send_input(&mut self, bytes: &[u8]) -> Result<(), String> {
        let mut input = self.stdin.take().ok_or("The process input is closed.")?;
        input.write_all(bytes).map_err(|err| format!("Could not send setup data: {err}"))?;
        Ok(())
    }

    pub fn wait_for<T>(
        &mut self, limit: Duration, cancel: &AtomicBool, mut parse: impl FnMut(&str) -> Result<Option<T>, String>,
    ) -> Result<T, String> {
        let deadline = Instant::now() + limit;
        while Instant::now() < deadline && !cancel.load(Ordering::Acquire) {
            match self.messages.recv_timeout(Duration::from_millis(100)) {
                Ok(line) => {
                    if let Some(value) = parse(&line)? {
                        return Ok(value);
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if self.closed.load(Ordering::Acquire) {
                        break;
                    }
                }
            }
        }
        if cancel.load(Ordering::Acquire) {
            return Err("The desktop operation was cancelled.".into());
        }
        match self.child.try_wait() {
            Ok(Some(status)) => Err(format!("The bundled process exited before reporting success ({status}).")),
            _ => Err("The bundled process did not report success before the timeout.".into()),
        }
    }

    pub fn wait_success(&mut self, limit: Duration, cancel: &AtomicBool) -> Result<(), String> {
        self.stdin.take();
        let deadline = Instant::now() + limit;
        while Instant::now() < deadline && !cancel.load(Ordering::Acquire) {
            match self.child.try_wait() {
                Ok(Some(status)) if status.success() => return Ok(()),
                Ok(Some(status)) => return Err(format!("The workspace process could not complete startup ({status}).")),
                Ok(None) => thread::sleep(Duration::from_millis(30)),
                Err(err) => return Err(format!("Could not check the workspace process: {err}")),
            }
        }
        Err("The workspace process did not finish before the timeout.".into())
    }

    pub fn terminate(&mut self) {
        self.stdin.take();
        let deadline = Instant::now() + Duration::from_secs(6);
        while Instant::now() < deadline {
            if self.child.try_wait().ok().flatten().is_some() {
                return;
            }
            thread::sleep(Duration::from_millis(40));
        }
        #[cfg(unix)]
        unsafe {
            // This child was spawned as the leader of its own process group.
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        #[cfg(windows)]
        unsafe {
            windows_sys::Win32::System::JobObjects::TerminateJobObject(self.job.as_raw_handle(), 1);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for ManagedChild {
    fn drop(&mut self) {
        self.terminate();
    }
}

#[cfg(windows)]
fn contain(child: &Child) -> Result<OwnedHandle, String> {
    use std::{mem::size_of, ptr::null};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let handle = CreateJobObjectW(null(), null());
        if handle.is_null() {
            return Err("Could not create a Windows job for the bundled process.".into());
        }
        let job = OwnedHandle::from_raw_handle(handle);
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job.as_raw_handle(), JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0 || AssignProcessToJobObject(job.as_raw_handle(), child.as_raw_handle()) == 0
        {
            return Err("Windows could not contain the bundled process in a job.".into());
        }
        Ok(job)
    }
}
