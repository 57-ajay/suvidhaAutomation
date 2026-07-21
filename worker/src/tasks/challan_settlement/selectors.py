"""vcourts.gov.in selectors — every one confirmed against the live DOM the
user captured (index.php dropdown/button, main.php police-pane form, results
span). engine.steps._PICK_JS resolves each selector to the LAST VISIBLE match,
so pane-scoped selectors stay correct even though hidden sibling panes carry
their own (invisible) captcha widgets.
"""

VC_HOME = "https://vcourts.gov.in/virtualcourt/index.php"

# ── index.php ────────────────────────────────────────────────────────────
# <select id="fstate_code" onchange="setStateCode(this.value)"> — the visible
# widget is a jQuery-"chosen" skin, but setting the underlying select's value
# and dispatching change (which select_by_text does) fires setStateCode, which
# is all Proceed Now needs.
SEL_DEPT_DROPDOWN = "select#fstate_code"
SEL_PROCEED = "button#payFineBTN"

# ── main.php ─────────────────────────────────────────────────────────────
SEL_TAB_POLICE = "a#mainMenuActive_police"  # "Challan/Vehicle No." tab
SEL_PANE = "#nav-sbPoliceStation"  # pane the tab activates

SEL_VEHICLE_INPUT = "input#vehicle_no"

# securimage captcha: <img id="captcha_image<rand>" alt="CAPTCHA Image">. The
# id suffix is random, so match by alt, scoped to the police pane.
SEL_CAPTCHA_IMAGE = '#nav-sbPoliceStation img[alt="CAPTCHA Image"]'
SEL_CAPTCHA_INPUT = "input#fcaptcha_code_police"
# Clicking the wrapper <a title="Refresh Image"> triggers both the securimage
# src refresh and clearCaptchaText().
SEL_CAPTCHA_REFRESH = '#nav-sbPoliceStation a[title="Refresh Image"]'

SEL_SUBMIT = '#nav-sbPoliceStation button[onclick="submitpoliceForm()"]'

# Results are AJAX-loaded into this span inside the same pane (no page reload).
SEL_RESULTS_SPAN = "#secondpagenew2"
