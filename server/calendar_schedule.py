"""Bounded calendar calculation using the documented corrected Croner version; dispatch stays in graph_clock."""
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FIELDS={'minutes':{'minutesInterval'},'hourly':{'hourlyMinute'},'daily':{'dailyTime'},
        'weekly':{'weeklyDay','weeklyDayTime'},'monthly':{'monthlyDay','monthlyTime'},'custom':{'cronExpression'}}
DAYS={'SUN':0,'MON':1,'TUE':2,'WED':3,'THU':4,'FRI':5,'SAT':6}


def integer(value,low,high,label):
    if type(value) is not int or not low<=value<=high:raise ValueError(f'{label} must be an integer from {low} to {high}.')
    return value


def time_parts(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',value):
        raise ValueError('Choose a time in HH:MM format.')
    return tuple(map(int,value.split(':')))


def expression(config):
    if not isinstance(config,dict):raise ValueError('Choose a calendar configuration.')
    mode=config.get('scheduleType')
    if not isinstance(mode,str) or mode not in FIELDS:raise ValueError('Choose a supported schedule frequency.')
    if set(config)!={'scheduleType','timezone'}|FIELDS[mode]:raise ValueError('Provide only the active schedule fields and time zone.')
    zone=config['timezone']
    if not isinstance(zone,str) or not zone or len(zone)>100:raise ValueError('Choose an IANA time zone.')
    try:ZoneInfo(zone)
    except (ValueError,ZoneInfoNotFoundError):raise ValueError('Choose an available IANA time zone.') from None
    if mode=='minutes':return '*/'+str(integer(config['minutesInterval'],1,60,'Minutes interval'))+' * * * *'
    if mode=='hourly':return str(integer(config['hourlyMinute'],0,59,'Hourly minute'))+' * * * *'
    if mode=='custom':
        value=config['cronExpression']
        if not isinstance(value,str) or not value.strip() or len(value)>256:raise ValueError('Choose a cron expression of at most 256 characters.')
        # Match the documented cron form, not Croner's one-off date constructor.
        if len(value.split()) not in (5,6):raise ValueError('Provide a five or six field cron expression.')
        return value.strip()
    field={'daily':'dailyTime','weekly':'weeklyDayTime','monthly':'monthlyTime'}[mode]
    hour,minute=time_parts(config[field])
    if mode=='daily':return f'{minute} {hour} * * *'
    if mode=='monthly':return f"{minute} {hour} {integer(config['monthlyDay'],1,31,'Monthly day')} * *"
    day=config['weeklyDay']
    if not isinstance(day,str) or day not in DAYS:raise ValueError('Choose a weekday from SUN through SAT.')
    return f'{minute} {hour} * * {DAYS[day]}'



def description(config):
    """Describe the immutable calendar definition, never its minute dispatch clock."""
    mode=config['scheduleType']
    if mode=='minutes':return f"at every {config['minutesInterval']}-minute mark within each hour"
    if mode=='hourly':return f"hourly at minute {config['hourlyMinute']:02d}"
    if mode=='daily':return f"daily at {config['dailyTime']}"
    if mode=='weekly':
        day={'SUN':'Sunday','MON':'Monday','TUE':'Tuesday','WED':'Wednesday','THU':'Thursday','FRI':'Friday','SAT':'Saturday'}[config['weeklyDay']]
        return f"every {day} at {config['weeklyDayTime']}"
    if mode=='monthly':return f"on day {config['monthlyDay']} of each month at {config['monthlyTime']}"
    return f"on cron schedule {config['cronExpression']}"


def preview(config,after,count=5):
    cron=expression(config)
    if type(after) not in (int,float) or not math.isfinite(after) or not 0<=after<=4102444800:
        raise ValueError('Choose a reference date between 1970 and 2100.')
    integer(count,1,8,'Preview count')
    node=shutil.which('node')
    if not node:raise ValueError('The installed calendar runtime is unavailable.')
    data=json.dumps(dict(expression=cron,timezone=config['timezone'],after=after,count=count),allow_nan=False).encode()
    try:
        result=subprocess.run([node,'--max-old-space-size=64',str(Path(__file__).with_name('calendar_worker.cjs'))],
                              input=data,capture_output=True,timeout=3,check=True)
        if len(result.stdout)>8192:raise ValueError('Calendar response exceeds its bound.')
        parsed=json.loads(result.stdout)
    except (OSError,subprocess.SubprocessError,json.JSONDecodeError):
        raise ValueError('The calendar runtime did not confirm an occurrence.') from None
    if not parsed.get('ok'):raise ValueError(parsed.get('error','Calendar validation failed.'))
    points=parsed['occurrences']
    if not isinstance(points,list) or not 1<=len(points)<=count or any(type(t) not in (int,float) or not math.isfinite(t) for t in points):
        raise ValueError('The calendar runtime returned invalid occurrences.')
    if any(a>=b for a,b in zip([after]+points,points)):raise ValueError('Calendar occurrences must advance.')
    return parsed
