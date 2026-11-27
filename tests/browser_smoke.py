"""Exercise the installed app from the signed-out landing page on a disposable instance."""
import os
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

opts = webdriver.ChromeOptions()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--window-size=1440,1000")
if os.environ.get("CHROME_BIN"):
    opts.binary_location = os.environ["CHROME_BIN"]
with webdriver.Chrome(service=Service(executable_path=os.environ.get("CHROMEDRIVER")), options=opts) as browser:
    wait = WebDriverWait(browser, 15)
    studio_url = os.environ.get("PENSTOCK_SMOKE_URL", "http://127.0.0.1:8765").rstrip("/")
    browser.get(studio_url)
    def find(id):
        return browser.find_element(By.ID, id)
    def click(id):
        wait.until(lambda _: find(id).is_displayed() and find(id).is_enabled())
        find(id).click()
    def contains(id, value):
        wait.until(lambda _: value.casefold() in find(id).text.casefold())
    find("owner").send_keys(os.environ["PENSTOCK_SMOKE_KEY"])
    find("unlock").find_element(By.TAG_NAME, "button").click()
    wait.until(lambda _: find("library").find_elements(By.TAG_NAME, "button"))
    find("library").find_element(By.XPATH, ".//button[contains(.,'Review an intake request')]").click()
    contains("name", "Review an intake request")
    wait.until(lambda _: len(find("canvas").find_elements(By.CLASS_NAME,"node")) == 5)
    click("test")
    contains("status","waiting")
    find("decision").find_element(By.XPATH,".//button[text()='Approve']").click()
    contains("status","finished")
    click("publish")
    contains("revision","Published")
    click("run")
    contains("status","waiting")
    find("decision").find_element(By.XPATH,".//button[text()='Decline']").click()
    contains("steps","held")
    if target := os.environ.get("PENSTOCK_SMOKE_SCREENSHOT"):
        browser.save_screenshot(str(Path(target)))
    click("manageData")
    find("resources").find_element(By.TAG_NAME,"summary").click()
    find("tableName").send_keys("Requests")
    find("columns").send_keys("Request,Status")
    click("createTable")
    contains("tableTitle","Requests")
    find("rowFields").find_element(By.TAG_NAME,"input").send_keys("Browser test")
    find("addRow").find_element(By.TAG_NAME,"button").click()
    contains("records","Browser test")
    timing=find("timingFields").find_element(By.TAG_NAME,"input")
    timing.clear()
    timing.send_keys("2")
    click("armTrigger")
    contains("triggers","enabled")
    def has_delivery(_):
        click("reloadResources")
        return find("deliveries").find_elements(By.TAG_NAME,"button")
    wait.until(has_delivery)
    find("triggers").find_element(By.TAG_NAME,"button").click()
    contains("triggers","paused")
    click("new")
    click("save")
    contains("revision","Draft")
    click("edit")
    contains("authorTitle", "Draft a workflow")
    working_name = "Browser recovery copy"
    def workflow_name():
        return find("proposal").find_element(By.XPATH, ".//label[text()='Workflow name']/input")
    editor_name = workflow_name()
    editor_name.clear()
    editor_name.send_keys(working_name)
    assert browser.execute_script(
        "return Object.keys(localStorage).filter(key=>key.startsWith('penstock.draft.v1/')).length"
    ) == 0, "Unsaved text must stay in memory before opt-in."
    find("author").find_element(By.XPATH, ".//label[text()='Keep this draft on this device']/input").click()
    find("draftAssistant").find_element(By.TAG_NAME, "summary").click()
    wait.until(lambda _: find("offline").is_displayed() or find("providerSetup").is_displayed())
    find("brief").send_keys("Locally retained test brief.")
    find("author").find_element(By.XPATH, ".//button[text()='Set up connections']").click()
    back = wait.until(lambda _: find("connectionsHub").find_element(By.XPATH, ".//button[text()='Return to this draft']"))
    back.click()
    wait.until(lambda _: find("author").is_displayed() and workflow_name().get_attribute("value") == working_name)
    assert find("brief").get_attribute("value") == "Locally retained test brief."
    assert not find("draftConsent").is_selected()
    browser.refresh()
    wait.until(lambda _: find("author").is_displayed() and workflow_name().get_attribute("value") == working_name)
    assert find("brief").get_attribute("value") == "Locally retained test brief."
    find("author").find_element(By.XPATH, ".//button[text()='Open saved version 1']").click()
    contains("name", "Review an intake request")
    browser.find_element(By.XPATH, "//nav//summary[text()='Working drafts on this device']").click()
    browser.find_element(By.XPATH, "//nav//button[text()='Restore working draft']").click()
    wait.until(lambda _: find("author").is_displayed() and workflow_name().get_attribute("value") == working_name)
    click("save")
    contains("name", working_name)
    assert browser.execute_script(
        "return Object.keys(localStorage).filter(key=>key.startsWith('penstock.draft.v1/')).length"
    ) == 0, "A successful save must clear the working copy."
    click("openStarters")
    wait.until(lambda _: not find("work").is_displayed())
    custom = find("guidedStarter").find_element(By.CSS_SELECTOR, ".guided-other-job")
    custom.find_element(By.TAG_NAME, "textarea").send_keys("Bring a project update to a team review")
    custom.find_element(By.TAG_NAME, "button").click()
    contains("authorTitle", "Plan your job")
    assert find("brief").get_attribute("value") == "Bring a project update to a team review"
    assert find("generate").get_attribute("disabled")
    click("manualJob")
    click("save")
    contains("name", "New workflow")
    click("test")
    contains("result", "Result")
    find("diagnosis").find_element(By.TAG_NAME, "summary").click()
    find("diagnosisContent").find_element(By.XPATH, ".//button[text()='Check selected step']").click()
    contains("diagnosisContent", "The selected step completed")
    click("openStarters")
    find("guidedStarter").find_element(By.XPATH, ".//button[text()='Open Weekly executive brief']").click()
    generate = wait.until(lambda _: find("guidedStarter").find_element(By.XPATH, ".//button[text()='Generate brief for review']"))
    generate.click()
    run_page = wait.until(lambda _: find("guidedStarter").find_element(By.CSS_SELECTOR, "[data-run-id]"))
    run = run_page.get_attribute("data-run-id")
    browser.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
        "width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True,
    })
    browser.get(studio_url + "/?notice=starter:" + run)
    approve = wait.until(lambda _: find("guidedStarter").find_element(By.XPATH, ".//button[text()='Approve']"))
    assert not find("workspaceNavigation").is_displayed()
    assert browser.execute_script(
        "const bounds=arguments[0].getBoundingClientRect(); return bounds.top >= 0 && bounds.bottom <= window.innerHeight",
        approve,
    ), "The review decision should be visible in the initial mobile viewport."
    assert browser.switch_to.active_element.tag_name == "h1"
    click("mobileNavigation")
    assert find("workspaceNavigation").is_displayed()
    assert browser.switch_to.active_element.get_attribute("id") == "project"
    browser.switch_to.active_element.send_keys(Keys.ESCAPE)
    wait.until(lambda _: not find("workspaceNavigation").is_displayed())
    assert browser.switch_to.active_element.get_attribute("id") == "mobileNavigation"
    find("guidedStarter").find_element(By.XPATH, ".//button[text()='Request changes']").click()
    find("guidedStarter").find_element(By.CSS_SELECTOR, ".guided-change-form textarea").send_keys("Use the next period")
    find("guidedStarter").find_element(By.XPATH, ".//button[text()='Send change request']").click()
    contains("guidedStarter", "Changes requested")
    browser.refresh()
    contains("guidedStarter", "Use the next period")
    find("guidedStarter").find_element(By.XPATH, ".//button[text()='Revise and preview']").click()
    contains("guidedStarter", "Set up a run")
    click("lock")
    wait.until(lambda _: find("owner").is_displayed())
    assert not browser.get_log("browser"), "Browser reported a console error"
