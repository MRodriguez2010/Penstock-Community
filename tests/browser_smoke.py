"""Exercise the installed app from the signed-out landing page on a disposable instance."""
import os
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

opts = webdriver.ChromeOptions()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--window-size=1440,1000")
with webdriver.Chrome(options=opts) as browser:
    wait = WebDriverWait(browser, 15)
    browser.get(os.environ.get("PENSTOCK_SMOKE_URL", "http://127.0.0.1:8765"))
    def find(id):
        return browser.find_element(By.ID, id)
    def click(id):
        wait.until(lambda _: find(id).is_displayed() and find(id).is_enabled())
        find(id).click()
    def contains(id, value):
        wait.until(lambda _: value.casefold() in find(id).text.casefold())
    find("owner").send_keys(os.environ["PENSTOCK_SMOKE_KEY"])
    find("unlock").find_element(By.TAG_NAME, "button").click()
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
    click("lock")
    wait.until(lambda _: find("owner").is_displayed())
    assert not browser.get_log("browser"), "Browser reported a console error"
