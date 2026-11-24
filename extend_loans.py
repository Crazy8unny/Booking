
import os
import re
import sys
import html
import urllib.parse
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright


# ================================================================
# CONFIGURATION — KEEP YOUR EXISTING VALUES
# ================================================================

# URLs — supplied through GitHub Variables
READER_URL = os.environ["READER_URL"]
LOANS_URL = os.environ["LOANS_URL"]

# Library login — supplied through GitHub Secrets
CREDENTIALS = {
    "username": os.environ["LIBRARY_USERNAME"],
    "password": os.environ["LIBRARY_PASSWORD"],
}

# Telegram — supplied through GitHub Secrets
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


# ================================================================
# STATUS MAPPING
# ================================================================

STATUS_MAP = {
    "הכותר מוזמן. הארכה לא התבצעה": {
        "type": "failed",
        "message": "הספר מוזמן, לא ניתן להאריך"
    },
    "ההארכה הושלמה, אך לא במלואה (עקב הגעה להארכה מקסימלית)": {
        "type": "success",
        "message": "ההארכה הושלמה (הגעת למכסת ההארכות)"
    },
}


def normalize_text(value):
    return re.sub(r"\s+", " ", (value or "")).strip()


def classify_status(status):
    """Classify the library website's extension status."""
    status = normalize_text(status)

    if not status:
        return "unknown", ""

    # Exact known mappings first
    for original, mapped in STATUS_MAP.items():
        if normalize_text(original) == status:
            return mapped["type"], mapped["message"]

    # Fallback matching for variations in website wording
    if "הארכה לא התבצעה" in status or "לא ניתן להאריך" in status:
        return "failed", status

    if "הושלמה" in status and "לא במלואה" in status:
        return "success", status

    return "unknown", status


# ================================================================
# LOGGING
# ================================================================

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


# ================================================================
# TELEGRAM
# ================================================================

def tg_send(message: str) -> bool:
    token = TELEGRAM_BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log("telegram: token/chat id not set - skipping notification")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")

    with urllib.request.urlopen(req, timeout=20) as resp:
        log(f"telegram: sent, status {resp.status}")

    return True


# ================================================================
# PLAYWRIGHT HELPERS
# ================================================================

def is_signed_in(page):
    try:
        return bool(page.locator("#login_button").first.evaluate(
            "el => (el && el.className ? String(el.className).includes('sq-signed-in') : false)"
        ))
    except Exception:
        return False


def js_click(page, selector):
    """Click with JS: popups can block Playwright's normal clicks."""
    page.evaluate(
        "(sel) => { const el = document.querySelector(sel); if (el) el.click(); }",
        selector
    )


def js_click_by_text(page, parent_selector, text):
    """Click the first matching button inside a parent element."""
    page.evaluate(
        """(sel, txt) => {
            const root = document.querySelector(sel) || document;
            const btns = Array.from(root.querySelectorAll('button'));
            const b = btns.find(x =>
                (x.textContent || '').trim().includes(txt)
            );
            if (b) b.click();
        }""",
        (parent_selector, text),
    )


# ================================================================
# LOANS TABLE
# ================================================================

DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")


def read_loans(page):
    """Read active loans, explicitly capturing the extension response cell."""

    result = page.evaluate("""
        (() => {
          const table = document.querySelector('#reader_form .agron-table.first')
                      || document.querySelector('.agron-table.first');
          if (!table) return {ok: false, rows: []};

          const rows = Array.from(table.querySelectorAll('tbody tr')).map(tr => {
            const out = [];

            tr.querySelectorAll('td').forEach(td => {
              const label = (td.getAttribute('data-th') || '').trim();
              const val = (td.textContent || '').trim();
              if (label) out.push([label, val]);
            });

            // Read the exact response field independently of generic cell parsing.
            const extensionCell = tr.querySelector('td[data-th="סטטוס ההארכה"]');
            const extensionStatus = extensionCell
              ? (extensionCell.textContent || '').trim()
              : '';

            return {cells: out, extension_status: extensionStatus};
          }).filter(row => row.cells.length);

          return {ok: true, rows};
        })()
    """)

    parsed = []

    for item in result.get("rows", []):
        row = _parse_loan_cells(item.get("cells", []))

        # The explicit extension response takes precedence over any other status cell.
        extension_status = normalize_text(item.get("extension_status", ""))
        if extension_status:
            row["status"] = extension_status

        if row.get("title"):
            parsed.append(row)

    return parsed


def _parse_loan_cells(cells):
    """Parse one table row into loan information."""

    out = {
        "title": "",
        "copy_no": "",
        "days_left": "",
        "status": "",
        "date_promise": "",
    }

    dates = []
    days = []
    title = ""
    status = ""

    last_idx = len(cells) - 1

    for idx, (label, val) in enumerate(cells):
        v = normalize_text(val)

        if not v:
            continue

        if normalize_text(label) == "סטטוס ההארכה":
            status = v
            continue

        if normalize_text(label) == "מספר עותק":
            out["copy_no"] = v
            continue

        if "סטטוס" in label:
            status = v
            continue

        dates.extend(DATE_RE.findall(v))

        if "עותק" in label or "copy" in label.lower():
            continue

        if re.fullmatch(r"\d+", v):
            days.append(v)

        elif idx == last_idx and not DATE_RE.search(v) and not status:
            status = v

        elif not DATE_RE.search(v) and len(v) > len(title):
            title = v

    out["title"] = title
    out["date_promise"] = dates[-1] if dates else ""
    out["days_left"] = days[0] if days else ""
    out["status"] = status

    return out


def parse_extension_response(page, response_data):
    """Parse the table_reader HTML returned by the extension AJAX request."""
    table_html = (response_data or {}).get("table_reader", "")
    if not table_html:
        raise RuntimeError("Extension AJAX response did not contain table_reader HTML")

    rows = page.evaluate("""html => {
        const doc = new DOMParser().parseFromString(html, 'text/html');
        return Array.from(doc.querySelectorAll('tbody tr')).map(tr => {
            const cell = label => {
                const td = Array.from(tr.querySelectorAll('td[data-th]'))
                    .find(x => (x.getAttribute('data-th') || '').trim() === label);
                return td ? (td.textContent || '').trim() : '';
            };
            return {
                title: cell('כותר'),
                copy_no: cell('מספר עותק'),
                date_promise: cell('תאריך מיועד להחזרה'),
                days_left: cell('ימים נותרים'),
                status: cell('סטטוס ההארכה')
            };
        }).filter(row => row.title);
    }""", table_html)

    return rows


# ================================================================
# LOAN HELPERS
# ================================================================

def rows_key(r):
    return str(
        r.get("copy_no")
        or r.get("copy")
        or r.get("id")
        or r.get("title")
    )


def due_short(r):
    due = (
        r.get("date_promise")
        or r.get("next_return")
        or ""
    ).strip()

    parts = [p for p in due.split("/") if p]

    return "/".join(parts[:2]) if parts else (due or "?")


def days_until_due(r, today=None):
    """Calculate days remaining from the due date using today's date."""
    due = (r.get("date_promise") or r.get("next_return") or "").strip()
    if not due:
        return None

    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            due_date = datetime.strptime(due, fmt).date()
            return (due_date - (today or datetime.now().date())).days
        except ValueError:
            continue
    return None


def is_urgent(r, today=None):
    days = days_until_due(r, today)
    return days is not None and 0 <= days <= 5


# ================================================================
# EXTENSION RESULT DETECTION
# ================================================================

def find_failed(before, after):
    """A loan failed if its due date did not move forward after extension."""
    after_map = {rows_key(row): row for row in (after or [])}
    failed = []

    for old_row in before or []:
        new_row = after_map.get(rows_key(old_row))

        # If the row disappeared or its due date stayed the same, count it as failed.
        old_due = (old_row.get("date_promise") or old_row.get("next_return") or "").strip()
        new_due = (new_row.get("date_promise") or new_row.get("next_return") or "").strip() if new_row else ""

        if not new_row or not old_due or new_due == old_due:
            failed.append(new_row or old_row)

    return failed


# ================================================================
# TELEGRAM MESSAGE
# ================================================================

def msg_entry(r, failed_keys, today):
    """Create a compact notification entry."""
    title = html.escape((r.get("title") or "?").strip())
    due = html.escape(due_short(r))
    days = days_until_due(r, today)
    days_text = str(days) if days is not None else "?"

    failed = rows_key(r) in failed_keys
    urgent = is_urgent(r, today)

    emoji = "🟠" if failed else "🟢"
    if urgent:
        emoji = "🚨 " + emoji

    extra = "\n└ ⚠️ נכשל: לא ניתן להאריך" if failed else ""
    return f"{emoji} <b>{title}</b> — {due} ({days_text} ימים){extra}\n"


def build_message(after, failed):
    """Build the Telegram notification using the current date at send time."""
    after = after or []
    today = datetime.now().date()
    failed_keys = {rows_key(r) for r in (failed or [])}

    failed_now = [r for r in after if rows_key(r) in failed_keys]
    urgent = [r for r in after if is_urgent(r, today)]
    ok_count = len(after) - len(failed_now)

    lines = [
        f"🦊 <b>ספריית אלקנה</b> | {today.strftime('%d.%m.%Y')}",
        f"📚 {len(after)} ספרים | 🟢 {ok_count} הוארכו | 🟠 {len(failed_now)} נכשלו | 🚨 {len(urgent)} דחופים",
        "",
    ]

    ordered = sorted(
        after,
        key=lambda r: (
            0 if is_urgent(r, today) else 1,
            0 if rows_key(r) in failed_keys else 1,
        ),
    )

    lines.extend(msg_entry(r, failed_keys, today) for r in ordered)
    return "\n".join(lines)


# ================================================================
# MAIN
# ================================================================

def main() -> int:

    username = CREDENTIALS["username"]
    password = CREDENTIALS["password"]

    error_msg = ""

    before = []
    after = []
    failed = []

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            viewport={"width": 1280, "height": 900}
        )

        page = context.new_page()
        page.set_default_timeout(30_000)

        try:

            # ----------------------------------------------------
            # 1. LOGIN
            # ----------------------------------------------------

            page.goto(READER_URL, wait_until="load")
            page.wait_for_timeout(2000)

            if is_signed_in(page):
                log("already signed in, continuing")

            else:
                log("signing in...")

                js_click(page, "#login_button")

                log(f"Current URL: {page.url}")
                log(f"Page title: {page.title()}")

                log(f"Login button count: {page.locator('#login_button').count()}")
                log(f"Login form count: {page.locator('#login_form').count()}")

                page.wait_for_timeout(3000)

                log("Page content after login click:")
                log(page.locator("body").inner_text()[:3000])

                page.screenshot(
                    path="debug-login.png",
                    full_page=True
                )

                page.wait_for_selector(
                    "#login_form",
                    timeout=10_000
                )

                page.fill(
                    "#login_form input[name='username']",
                    username
                )

                page.fill(
                    "#login_form input[name='password']",
                    password
                )

                rid = page.locator(
                    "#login_form input[name='readerid']"
                )

                if rid.count() and rid.first.is_visible():
                    page.fill(
                        "#login_form input[name='readerid']",
                        ""
                    )

                js_click(
                    page,
                    "#login_form button[type='submit']"
                )

                page.wait_for_timeout(6000)

                if not is_signed_in(page):
                    raise RuntimeError("login did not complete")

                try:
                    page.wait_for_selector(
                        ".privacy-popup",
                        state="visible",
                        timeout=5000
                    )

                    page.evaluate("""
                        () => {
                            const a = document.querySelector(
                                '#privacy_policy #accept'
                            );
                            if (a) a.checked = true;
                        }
                    """)

                    js_click(page, "#privacy_policy button")

                except Exception:
                    pass

            log("signed in OK")

            # ----------------------------------------------------
            # 2. OPEN LOANS PAGE
            # ----------------------------------------------------

            page.goto(LOANS_URL, wait_until="load")

            page.wait_for_selector(
                "#reader_form input[name='cid[]']",
                timeout=20_000
            )

            page.wait_for_selector(
                "#reader_form .agron-table.first tbody tr",
                timeout=20_000
            )

            page.wait_for_timeout(2500)

            before = read_loans(page)

            log(f"found {len(before)} active loans")

            # ----------------------------------------------------
            # 3. SELECT LOANS
            # ----------------------------------------------------

            boxes = page.locator(
                "#reader_form input[name='cid[]']"
            )

            checked = 0

            for i in range(boxes.count()):

                if not boxes.nth(i).is_checked():
                    boxes.nth(i).check()
                    checked += 1

            if checked == 0:

                log("no new loans to extend")

                after = before

            else:

                # ------------------------------------------------
                # 4. EXTEND LOANS
                # ------------------------------------------------

                log("clicking 'הארכת השאלה'...")

                page.locator(
                    '#reader_form button:has-text("הארכת השאלה")'
                ).wait_for(timeout=10_000)

                # The site returns the updated table inside a JSON response
                # (response["table_reader"]), not necessarily in the existing DOM table.
                def is_extension_ajax_response(response):
                    if response.request.method != "POST":
                        return False
                    try:
                        data = response.json()
                        return isinstance(data, dict) and "table_reader" in data
                    except Exception:
                        return False

                try:
                    with page.expect_response(
                        is_extension_ajax_response, timeout=20_000
                    ) as response_info:
                        js_click_by_text(
                            page,
                            "#reader_form",
                            "הארכת השאלה"
                        )

                    ajax_response = response_info.value
                    response_data = ajax_response.json()
                    log(f"received extension AJAX response | HTTP {ajax_response.status}")
                    after = parse_extension_response(page, response_data)

                except Exception as e:
                    log(f"warning: could not capture/parse extension AJAX response: {e}")
                    log("falling back to reading the current page table")
                    page.wait_for_timeout(2000)
                    after = read_loans(page)

                # ------------------------------------------------
                # 5. READ RESULTS
                # ------------------------------------------------

                for row in after:
                    log(f"extension status | {row.get('title', '?')} | {row.get('status') or '[empty]'}")

                failed = find_failed(before, after)

                log("extension request submitted")

                for ln in build_message(after, failed).splitlines():
                    log(ln)

        except Exception as e:

            error_msg = str(e)

            log(f"ERROR: {e}")

        finally:

            browser.close()

    # ------------------------------------------------------------
    # 6. TELEGRAM NOTIFICATION
    # ------------------------------------------------------------

    message = build_message(after, failed)

    try:

        if error_msg:
            tg_send(
                "🔴 <b>ספריית אלקנה — שגיאה!</b>\n"
                f"<code>{html.escape(error_msg)}</code>"
            )
        else:
            tg_send(build_message(after, failed))
            
        for ln in message.splitlines():
            log("| " + ln)

    except Exception as e:

        log(f"telegram send error: {e}")

    return 1 if error_msg else 0


if __name__ == "__main__":
    sys.exit(main())