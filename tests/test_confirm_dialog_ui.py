"""Real-browser tests for the shared confirmation dialog (confirmAction in frontend/js/utils.js) that
replaced window.confirm for destructive actions: a labelled modal <dialog> whose Cancel has focus
first, Escape and a backdrop click cancel, Confirm resolves true, focus returns to the control that
opened it, and cancelling a document delete sends no request. Desktop (1280px) and phone (390px).
Skipped without Chrome.
"""

import unittest

from tests.test_home_continue_learning_ui import MOCK as HOME_MOCK
from tests.test_quiz_player_ui import find_chrome
import tests.test_sidebar_navigation_ui as sidebar_harness

MOCK = HOME_MOCK + r"""
window.__deletes = [];
const confirmFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  if ((init.method || "GET").toUpperCase() === "DELETE") {
    window.__deletes.push(p);
    if (window.__deleteHold) await window.__deleteHold;
    return json({detail: "Storage is unavailable."}, 503);   // every delete fails in this test
  }
  return confirmFetch(input, init);
};
"""

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const dialog = () => document.querySelector("dialog.confirm-dialog");
const describe = () => {
  const d = dialog();
  if (!d) return null;
  const labelled = document.getElementById(d.getAttribute("aria-labelledby"));
  const described = document.getElementById(d.getAttribute("aria-describedby"));
  return {open: d.open, modal: d.matches(":modal"), title: labelled?.textContent || null, message: described?.textContent || null,
    focus: document.activeElement?.textContent || null, buttons: [...d.querySelectorAll("button")].map((b) => b.textContent),
    overflow: d.getBoundingClientRect().right > innerWidth + 1};
};
(async () => {
  await sleep(1500);
  const opener = document.createElement("button");
  opener.textContent = "Opener"; document.querySelector(".home-page").prepend(opener); opener.focus();

  // Escape cancels and focus returns to the opener.
  let pending = confirmAction({title: "Delete this?", message: "It cannot be recovered.", confirmLabel: "Delete it"});
  await sleep(100);
  out.opened = describe();
  dialog().dispatchEvent(new Event("cancel", {cancelable: true}));
  out.escape = {result: await pending, gone: !dialog(), focusBack: document.activeElement === opener};

  // A click on the backdrop (the dialog element itself, outside its card) cancels; a click inside does not.
  pending = confirmAction({title: "Delete again?"});
  await sleep(100);
  dialog().querySelector(".confirm-dialog-title").click(); await sleep(50);
  const stillOpen = Boolean(dialog()?.open);
  dialog().click();
  out.backdrop = {stillOpenAfterInsideClick: stillOpen, result: await pending};

  // Confirm resolves true.
  pending = confirmAction({title: "Delete for real?"});
  await sleep(100);
  dialog().querySelector(".confirm-dialog-confirm").click();
  out.confirm = {result: await pending, gone: !dialog()};

  // A real call site: Delete on a Home document card asks first; Cancel sends no request.
  const remove = document.querySelector('.home-doc-card[data-document-id="mkt.pdf"] .home-doc-delete');
  remove.focus(); remove.click(); await sleep(150);
  out.documentDelete = describe();
  dialog().querySelector(".confirm-dialog-cancel").click(); await sleep(150);
  out.documentCancelled = {deletes: [...window.__deletes], focusBack: document.activeElement === remove, buttonEnabled: !remove.disabled};

  // Confirmed while the request is slow: repeated clicks and a second Delete send one request; the
  // API failure is reported as a failure (never "Deleted") and the document stays.
  let release; window.__deleteHold = new Promise((resolve) => { release = resolve; });
  remove.click(); await sleep(150);
  const confirmButton = dialog().querySelector(".confirm-dialog-confirm");
  confirmButton.click(); confirmButton.click(); await sleep(100);
  const whileDeleting = {disabled: remove.disabled, text: remove.textContent, dialogs: document.querySelectorAll("dialog.confirm-dialog").length};
  remove.click(); deleteUploadedSource({title: "mkt.pdf"}, remove); await sleep(150);
  const secondAsk = Boolean(dialog());
  window.__deleteHold = null; release(); await sleep(600);
  out.documentFailed = {...whileDeleting, secondAsk, deletes: window.__deletes.length, toast: document.getElementById("toast").textContent,
    stillListed: Boolean(document.querySelector('.home-doc-card[data-document-id="mkt.pdf"]')), buttonEnabled: !remove.disabled,
    buttonText: remove.textContent};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


def run_at_width(width, height):
    original = (sidebar_harness.MOCK, sidebar_harness.DRIVER)
    sidebar_harness.MOCK, sidebar_harness.DRIVER = MOCK, DRIVER
    try:
        return sidebar_harness.run_at_width(width, height)
    finally:
        sidebar_harness.MOCK, sidebar_harness.DRIVER = original


class ConfirmDialogAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_is_a_labelled_modal_with_cancel_focused_first(self):
        opened = self.out["opened"]
        self.assertTrue(opened["open"] and opened["modal"])
        self.assertEqual((opened["title"], opened["message"]), ("Delete this?", "It cannot be recovered."))
        self.assertEqual(opened["buttons"], ["Cancel", "Delete it"])
        self.assertEqual(opened["focus"], "Cancel")
        self.assertFalse(opened["overflow"])

    def test_escape_cancels_and_returns_focus(self):
        self.assertEqual(self.out["escape"], {"result": False, "gone": True, "focusBack": True})

    def test_backdrop_click_cancels_but_a_click_inside_does_not(self):
        backdrop = self.out["backdrop"]
        self.assertTrue(backdrop["stillOpenAfterInsideClick"])
        self.assertFalse(backdrop["result"])

    def test_confirm_resolves_true(self):
        self.assertEqual(self.out["confirm"], {"result": True, "gone": True})

    def test_deleting_a_document_asks_first_and_cancel_sends_nothing(self):
        asked = self.out["documentDelete"]
        self.assertEqual(asked["title"], 'Delete "mkt.pdf"?')
        self.assertIn("cannot be undone", asked["message"])
        self.assertEqual(asked["buttons"], ["Cancel", "Delete document"])
        self.assertEqual(self.out["documentCancelled"], {"deletes": [], "focusBack": True, "buttonEnabled": True})

    def test_a_confirmed_delete_is_sent_once_and_a_failure_is_reported_as_a_failure(self):
        failed = self.out["documentFailed"]
        self.assertEqual((failed["disabled"], failed["text"], failed["dialogs"]), (True, "Deleting", 0))
        self.assertFalse(failed["secondAsk"])          # no second confirmation while the first delete runs
        self.assertEqual(failed["deletes"], 1)          # double click on Confirm + a second Delete: one request
        self.assertEqual(failed["toast"], "Delete failed: Storage is unavailable.")
        self.assertTrue(failed["stillListed"])
        self.assertEqual((failed["buttonEnabled"], failed["buttonText"]), (True, "Delete"))


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ConfirmDialogDesktopTests(ConfirmDialogAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ConfirmDialogPhoneTests(ConfirmDialogAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 800)
