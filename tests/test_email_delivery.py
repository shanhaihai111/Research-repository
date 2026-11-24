import json, logging, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
import email_delivery as mail

class FakeSMTP:
    def __init__(self,*args,**kwargs): self.sent=[]
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def login(self,user,password): return (235,b"ok")
    def send_message(self,msg,from_addr=None,to_addrs=None): self.sent.append((msg,from_addr,to_addrs)); return {}

class FakeRejected(FakeSMTP):
    def send_message(self,msg,from_addr=None,to_addrs=None): return {to_addrs[0]:(550,b"mailbox unavailable")}

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)
        (self.path/"Weekly_Research_Report_20261115.pdf").write_bytes(b"%PDF-1.7\nbody\n%%EOF\n")
        (self.path/"Weekly_Research_Report_20261115.html").write_text("<!doctype html><html><body>ok</body></html>",encoding="utf-8")
        self.env={"SMTP_HOST":"smtp.example.org","SMTP_PORT":"465","SMTP_USER":"sender@example.org","SMTP_FROM":"sender@example.org","SMTP_PASSWORD":"DO_NOT_LOG_THIS","REPORT_RECIPIENT":"reader@163.com","REPORT_EXPECTED_RECIPIENT":"reader@163.com"}
    def tearDown(self): self.tmp.cleanup()
    def test_headers_and_smtp_accepted(self):
        fake=FakeSMTP()
        with patch.object(mail.smtplib,"SMTP_SSL",return_value=fake):
            result=mail.deliver_report(self.path,"20261115","2026-11-08","2026-11-14",2,self.env,True,True)
        self.assertEqual(result["status"],"SMTP_ACCEPTED"); self.assertFalse(result["final_delivery_confirmed"])
        msg=fake.sent[0][0]
        for h in ("From","To","Subject","Date","Message-ID"): self.assertTrue(msg[h])
        self.assertEqual(len(msg.get_payload()),3)
        self.assertEqual(len(result["attachments"]),2)
    def test_refusal_is_not_success(self):
        with patch.object(mail.smtplib,"SMTP_SSL",return_value=FakeRejected()):
            result=mail.deliver_report(self.path,"20261115","a","b",2,self.env,True,True)
        self.assertEqual(result["status"],"REJECTED")
        self.assertIn("***@163.com",result["recipient_refusals"][0]["recipient_masked"])
    def test_preview_does_not_connect(self):
        with patch.object(mail.smtplib,"SMTP_SSL",side_effect=AssertionError("network must not run")):
            result=mail.deliver_report(self.path,"20261115","a","b",2,self.env,False,False)
        self.assertEqual(result["status"],"PREVIEW_ONLY")
        self.assertEqual(result["smtp_submission"],"not_attempted")
    def test_password_not_logged(self):
        logger=logging.getLogger("weekly_report.email")
        with self.assertLogs(logger,level="INFO") as captured:
            with patch.object(mail.smtplib,"SMTP_SSL",side_effect=RuntimeError("broken " + self.env["SMTP_PASSWORD"])):
                result=mail.deliver_report(self.path,"20261115","a","b",2,self.env,True,True)
        self.assertNotIn(self.env["SMTP_PASSWORD"],"\n".join(captured.output)+json.dumps(result))
        self.assertEqual(result["status"],"FAILED")
    def test_sender_must_match_authenticated_account(self):
        self.env["SMTP_FROM"]="other@example.org"
        with patch.object(mail.smtplib,"SMTP_SSL") as smtp:
            result=mail.deliver_report(self.path,"20261115","a","b",2,self.env,True,True)
        smtp.assert_not_called(); self.assertEqual(result["status"],"FAILED")
    def test_recipient_must_be_single_address(self):
        self.env["REPORT_RECIPIENT"]="reader@163.com, extra@example.org"
        with patch.object(mail.smtplib,"SMTP_SSL") as smtp:
            result=mail.deliver_report(self.path,"20261115","a","b",2,self.env,True,True)
        smtp.assert_not_called(); self.assertEqual(result["status"],"FAILED")
    def test_recipient_must_match_expected(self):
        self.env["REPORT_RECIPIENT"]="wrong@163.com"
        with patch.object(mail.smtplib,"SMTP_SSL") as smtp:
            result=mail.deliver_report(self.path,"20261115","a","b",2,self.env,True,True)
        smtp.assert_not_called(); self.assertEqual(result["status"],"FAILED")
    def test_duplicate_guard_blocks_legacy_sent_log(self):
        p=self.path/"prior.json"; p.write_text('{"email_status":"sent"}',encoding="utf-8")
        self.assertTrue(mail.previously_attempted(p))
    def test_duplicate_guard_allows_preview_only(self):
        p=self.path/"prior.json"; p.write_text('{"email_status":"PREVIEW_ONLY","smtp_submission":"not_attempted"}',encoding="utf-8")
        self.assertFalse(mail.previously_attempted(p))
    def test_bad_pdf_blocks_smtp(self):
        (self.path/"Weekly_Research_Report_20261115.pdf").write_bytes(b"not pdf")
        with patch.object(mail.smtplib,"SMTP_SSL") as smtp:
            result=mail.deliver_report(self.path,"20261115","a","b",2,self.env,True,True)
        smtp.assert_not_called(); self.assertEqual(result["status"],"FAILED")

if __name__=="__main__": unittest.main()
