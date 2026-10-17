import os
import json
import sqlite3
import datetime
import smtplib
import ssl
import csv
import io
import glob
import hmac
import base64
from email.message import EmailMessage

import anthropic
import streamlit as st

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "outreach.db")
MODEL_DEFAULT = "claude-opus-5-5"
RESUME_BASENAME = os.path.join(APP_DIR, "my_resume")
RESUME_TYPES = {"pdf": "application/pdf", "txt": "text/plain", "md": "text/markdown"}

st.set_page_config(page_title="AI Outreach Agent", page_icon="🤝", layout="wide")

def cfg(name, default=""):
    """Read a setting from the environment, then from Streamlit secrets."""
    value = os.getenv(name)
    if value:
        return value
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default

def is_local_request():
    host = (st.context.headers.get("Host") or "").rsplit(":", 1)[0]
    return host in ("localhost", "127.0.0.1", "[::1]")

def require_password():
    # Set PASSWORD_REQUIRED="false" only when the app is private on Streamlit Cloud (viewers invited by email).
    if cfg("PASSWORD_REQUIRED", "true").strip().lower() == "false":
        return
    expected = cfg("APP_PASSWORD")
    if not expected:
        if is_local_request():
            return
        st.error("This deployment has no APP_PASSWORD. Add one in the app's Secrets before using it.")
        st.stop()
    if st.session_state.get("authed"):
        return
    st.title("🤝 AI Outreach Agent")
    entered = st.text_input("Password", type="password")
    if entered:
        if hmac.compare_digest(entered.encode(), expected.encode()):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()

require_password()

STATUSES = ["New", "Draft ready", "Approved", "Sent", "Replied", "Follow-up due", "Not interested", "Closed"]
KINDS = ["Job", "Sponsorship"]
CHANNELS = ["X DM (manual)", "Email"]

def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            company TEXT NOT NULL,
            person TEXT DEFAULT '',
            role_or_product TEXT DEFAULT '',
            channel TEXT NOT NULL DEFAULT 'X DM (manual)',
            profile_url TEXT DEFAULT '',
            contact_email TEXT DEFAULT '',
            context TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'New',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(kind, company, person, channel)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            subject TEXT DEFAULT '',
            body TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,
            sent_at TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(lead_id) REFERENCES leads(id)
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value BLOB)")
    lead_cols = {r["name"] for r in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if "research" not in lead_cols:
        conn.execute("ALTER TABLE leads ADD COLUMN research TEXT DEFAULT ''")
    conn.commit()
    return conn

def now():
    return datetime.datetime.now().isoformat(timespec="seconds")

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}

RESEARCH_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
]

def get_setting(key, default=None):
    conn = connect_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = connect_db()
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()

def delete_setting(key):
    conn = connect_db()
    conn.execute("DELETE FROM settings WHERE key=?", (key,))
    conn.commit()
    conn.close()

def save_resume(filename, data):
    ext = filename.rsplit(".", 1)[-1].lower()
    set_setting("resume_ext", ext)
    set_setting("resume_data", sqlite3.Binary(data))

def get_resume():
    """Return (extension, bytes) for the saved resume, or None. Stored in the DB so backups include it."""
    data = get_setting("resume_data")
    if data is None:
        # Earlier versions saved the resume as my_resume.<ext> next to app.py.
        matches = glob.glob(RESUME_BASENAME + ".*")
        if not matches:
            return None
        with open(matches[0], "rb") as f:
            save_resume(matches[0], f.read())
        os.remove(matches[0])
        return get_resume()
    return get_setting("resume_ext"), bytes(data)

def delete_resume():
    delete_setting("resume_ext")
    delete_setting("resume_data")

def resume_blocks():
    """Content blocks that hand the saved resume to Claude, or [] if none is saved."""
    resume = get_resume()
    if not resume:
        return []
    ext, data = resume
    if ext == "pdf":
        return [{
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(data).decode("utf-8")},
            "title": "Sender's resume",
        }]
    return [{"type": "text", "text": "Sender's resume:\n" + data.decode("utf-8", errors="replace")}]

def call_claude(content, api_key, model, tools=None, json_schema=None, effort="medium"):
    """Run one Claude request (resuming server-tool pauses) and return the joined text output."""
    client = anthropic.Anthropic(api_key=api_key or None)
    output_config = {"effort": effort}
    if json_schema:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    messages = [{"role": "user", "content": content}]
    extra = {"tools": tools} if tools else {}
    try:
        for _ in range(4):
            response = client.beta.messages.create(
                model=model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config=output_config,
                messages=messages,
                **extra,
            )
            if response.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": response.content}]
    except anthropic.AuthenticationError:
        raise RuntimeError("Invalid Anthropic API key.")
    except anthropic.RateLimitError:
        raise RuntimeError("Rate limited by the Claude API. Try again shortly.")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise RuntimeError("Could not reach the Claude API. Check your connection.")
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request.")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("Claude returned no text. Try again.")
    return text

def parse_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(t)

def add_lead(data):
    conn = connect_db()
    try:
        cur = conn.execute("""
            INSERT INTO leads(kind, company, person, role_or_product, channel, profile_url,
                              contact_email, context, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data["kind"], data["company"].strip(), data.get("person", "").strip(),
            data.get("role_or_product", "").strip(), data["channel"],
            data.get("profile_url", "").strip(), data.get("contact_email", "").strip(),
            data.get("context", "").strip(), data.get("status", "New"), now(), now()
        ))
        conn.commit()
        return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, "Possible duplicate: this kind/company/person/channel combination already exists."
    finally:
        conn.close()

def fetch_leads(kind_filter="All"):
    conn = connect_db()
    if kind_filter == "All":
        rows = conn.execute("SELECT * FROM leads ORDER BY updated_at DESC, id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM leads WHERE kind=? ORDER BY updated_at DESC, id DESC", (kind_filter,)).fetchall()
    conn.close()
    return rows

def get_lead(lead_id):
    conn = connect_db()
    row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    conn.close()
    return row

def update_lead(lead_id, **fields):
    allowed = {"status", "context", "contact_email", "profile_url", "channel", "research"}
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = now()
    sql = ", ".join([f"{k}=?" for k in fields])
    conn = connect_db()
    conn.execute(f"UPDATE leads SET {sql} WHERE id=?", (*fields.values(), lead_id))
    conn.commit()
    conn.close()

def save_draft(lead_id, subject, body):
    conn = connect_db()
    cur = conn.execute(
        "INSERT INTO drafts(lead_id, subject, body, approved, created_at) VALUES(?,?,?,?,?)",
        (lead_id, subject, body, 0, now())
    )
    conn.execute("UPDATE leads SET status='Draft ready', updated_at=? WHERE id=?", (now(), lead_id))
    conn.commit()
    draft_id = cur.lastrowid
    conn.close()
    return draft_id

def fetch_drafts(lead_id=None):
    conn = connect_db()
    if lead_id:
        rows = conn.execute("""
            SELECT drafts.*, leads.company, leads.person, leads.kind, leads.channel,
                   leads.contact_email, leads.profile_url, leads.status AS lead_status
            FROM drafts JOIN leads ON leads.id=drafts.lead_id
            WHERE drafts.lead_id=? ORDER BY drafts.id DESC
        """, (lead_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT drafts.*, leads.company, leads.person, leads.kind, leads.channel,
                   leads.contact_email, leads.profile_url, leads.status AS lead_status
            FROM drafts JOIN leads ON leads.id=drafts.lead_id
            ORDER BY drafts.id DESC
        """).fetchall()
    conn.close()
    return rows

def set_draft(draft_id, **fields):
    allowed = {"subject", "body", "approved", "sent_at"}
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return
    sql = ", ".join([f"{k}=?" for k in fields])
    conn = connect_db()
    conn.execute(f"UPDATE drafts SET {sql} WHERE id=?", (*fields.values(), draft_id))
    conn.commit()
    conn.close()

def mark_sent(draft_id, lead_id):
    conn = connect_db()
    conn.execute("UPDATE drafts SET sent_at=? WHERE id=?", (now(), draft_id))
    conn.execute("UPDATE leads SET status='Sent', updated_at=? WHERE id=?", (now(), lead_id))
    conn.commit()
    conn.close()

def send_email_smtp(to_addr, subject, body, attachment=None, attachment_name=None):
    """attachment is an (extension, bytes) tuple, e.g. from get_resume()."""
    host = cfg("SMTP_HOST")
    port = int(cfg("SMTP_PORT", "587") or "587")
    user = cfg("SMTP_USERNAME")
    password = cfg("SMTP_PASSWORD")
    sender = cfg("SMTP_FROM") or user
    if not all([host, user, password, sender]):
        raise RuntimeError("Email sending is not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM in your environment or Streamlit secrets.")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(body)
    if attachment:
        ext, data = attachment
        maintype, subtype = RESUME_TYPES.get(ext, "application/octet-stream").split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=attachment_name or f"Resume.{ext}")
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)

def lead_facts(lead):
    return f"""Lead type: {lead['kind']}
Company: {lead['company']}
Person: {lead['person']}
Role/product: {lead['role_or_product']}
Public profile/company URL: {lead['profile_url']}
Context supplied by user: {lead['context']}"""

def research_prompt(lead, profile):
    return f"""Research this outreach lead so the sender can write a well-informed, one-to-one message.
{lead_facts(lead)}
Sender profile: {json.dumps(profile, ensure_ascii=False)}
The sender's resume, if provided, is attached above.

Use web search, and fetch the profile/company URL if one is given. Then write a brief in Markdown with exactly these sections:
## Who they are
What the person and company do, their focus areas, and recent public work (posts, launches, funding, open roles). Say plainly when something could not be verified.
## What they likely need
Problems, hiring needs, or gaps that are evident from public information. Label inferences as inferences.
## How the sender fits
Map specific resume/profile items to those needs. Be honest about gaps.
## Ideas the sender could build for them
3-5 concrete ideas, each scoped to a weekend-to-two-week prototype: what it is, why it matters to them, and which of the sender's skills it uses.
## Outreach hooks
1-3 specific, verifiable openers for the message.

Only state facts you found or that were supplied. Do not invent metrics, relationships, or hiring plans. Do not include private or sensitive personal information."""

TONE_GUIDES = {
    "Unhinged (bold & memorable)": (
        "Write like a sharp, funny builder who refuses to send a boring email. Open with a pattern-interrupt line that "
        "could only be about this recipient (no 'I hope this finds you well'). Use self-aware humor, playful confidence, "
        "short punchy sentences, and one slightly absurd-but-true line about the sender's work. The subject line should "
        "make them curious enough to open it. It must still read as a real person who can do the job: no insults, no "
        "profanity, no begging, no fake drama, and every claim stays true."
    ),
    "Warm and professional": "Warm, polished, and professional.",
    "Direct and concise": "Direct and concise; no filler.",
    "Friendly and conversational": "Friendly and conversational, like a note to a peer.",
}

def run_research(lead, profile, api_key, model):
    brief = call_claude(
        resume_blocks() + [{"type": "text", "text": research_prompt(lead, profile)}],
        api_key, model, tools=RESEARCH_TOOLS,
    )
    update_lead(lead["id"], research=brief)
    return brief

def draft_prompt(lead, profile, attach_resume):
    attach_note = "The sender's resume will be attached to this email; mention it briefly." if attach_resume else "No resume is attached; do not claim one is."
    return f"""You are helping write one-to-one professional outreach. Return JSON with "subject" (empty for a DM) and "body".
{lead_facts(lead)}
Channel: {lead['channel']}.
Research brief on this lead (may be empty):
{lead['research'] or '(none)'}
Sender profile: {json.dumps(profile, ensure_ascii=False)}
The sender's resume, if provided, is attached above.
Voice: {TONE_GUIDES.get(profile.get('tone'), profile.get('tone'))}
Rules:
- Be specific, honest, and human. Lead with one concrete hook from the research brief when there is one.
- Show the sender's work, not adjectives: name 1-3 specific projects, products, or results from the resume (with their real numbers, if the resume has them) and tie each to something this recipient cares about.
- Do not invent knowledge of the contact, product, hiring plans, follower count, campaign performance, or previous relationship.
- Reference only experience that appears in the resume or sender profile.
- For job leads with an email channel, write it as a short application: the role, 2-3 relevant proof points from the resume, and one idea the sender could build for them, framed as an offer. {attach_note}
- For job leads over DM, ask about the role or a referral and mention one build idea in a sentence.
- For sponsorship leads, propose a relevant paid collaboration without promising unverified reach or results.
- For DMs, keep it under 600 characters and do not add a subject.
- No manipulative urgency, mass-mail wording, or false claims.
- The user must review and approve before sending."""

def csv_template():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["kind","company","person","role_or_product","channel","profile_url","contact_email","context"])
    w.writerow(["Job","Example AI Co","Recruiter name","GenAI Engineer","Email","https://example.com/profile","recruiter@example.com","Hiring for LLM applications; mention relevant experience"])
    w.writerow(["Sponsorship","Example DevTool","Founder name","Developer tool sponsorship","X DM (manual)","https://x.com/example","","AI developer product; pitch a sponsored technical walkthrough"])
    return out.getvalue()

st.title("🤝 AI Outreach Agent")
st.caption("Private outreach workspace for job opportunities and sponsorships. Drafts require your approval. X DMs are prepared for manual sending only.")

with st.sidebar:
    st.header("AI setup")
    # Never pre-fill the key into the widget: widget values are sent to the browser.
    secret_key = cfg("ANTHROPIC_API_KEY")
    api_key = st.text_input(
        "Anthropic API key", type="password",
        placeholder="Using the key from secrets" if secret_key else "",
    ) or secret_key
    model = st.text_input("Claude model", value=cfg("CLAUDE_MODEL", MODEL_DEFAULT) or MODEL_DEFAULT)
    st.markdown("[Get an Anthropic API key](https://platform.claude.com/settings/keys)")
    st.divider()
    st.header("Your profile")
    saved_profile = json.loads(get_setting("profile", "{}"))
    profile_name = st.text_input("Name", value=saved_profile.get("name", cfg("OUTREACH_NAME")))
    profile_summary = st.text_area(
        "Career / creator summary",
        value=saved_profile.get("career_creator_summary", cfg("OUTREACH_PROFILE", "I work on GenAI, agentic AI, LLM applications, backend systems, and developer-focused products. I create content for a tech audience on X.")),
        height=150
    )
    skills = st.text_area("Relevant skills / proof points", value=saved_profile.get("skills_and_proof_points", "Python, backend systems, GenAI, agentic AI, LLM applications, RAG, developer tools"), height=90)
    audience = st.text_area("Audience / sponsorship proof points", value=saved_profile.get("audience_proof_points", "Share current X follower count, average impressions, audience geography, engagement, and past campaign results here. Do not invent metrics."), height=90)
    tones = list(TONE_GUIDES)
    tone = st.selectbox("Message tone", tones, index=tones.index(saved_profile["tone"]) if saved_profile.get("tone") in tones else 0)
    if st.button("Save profile"):
        set_setting("profile", json.dumps({
            "name": profile_name, "career_creator_summary": profile_summary,
            "skills_and_proof_points": skills, "audience_proof_points": audience, "tone": tone,
        }))
        st.success("Profile saved.")
    st.divider()
    st.header("Your resume")
    resume_upload = st.file_uploader("Upload resume (PDF, TXT, or MD)", type=list(RESUME_TYPES))
    if resume_upload is not None:
        current = get_resume()
        new_bytes = resume_upload.getvalue()
        if not current or current[1] != new_bytes:
            save_resume(resume_upload.name, new_bytes)
    saved_resume = get_resume()
    if saved_resume:
        st.success(f"Resume saved ({saved_resume[0].upper()}). Claude reads it for research and drafts, and it can be attached to emails.")
        if st.button("Remove saved resume"):
            delete_resume()
            st.rerun()
    else:
        st.info("No resume saved yet.")
    st.caption("Profile, resume, leads and drafts are stored in outreach.db. On Streamlit Cloud that file is wiped when the app restarts, so download a backup from the Import / export tab. The LLM receives the context you submit when generating a draft.")

profile = {
    "name": profile_name,
    "career_creator_summary": profile_summary,
    "skills_and_proof_points": skills,
    "audience_proof_points": audience,
    "tone": tone
}

leads_tab, draft_tab, data_tab = st.tabs(["1 · Leads", "2 · Drafts & sending", "3 · Import / export"])

with leads_tab:
    st.subheader("Add a lead")
    with st.form("add_lead_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            kind = st.selectbox("Pipeline", KINDS)
            company = st.text_input("Company / organization *")
            person = st.text_input("Contact person")
            role_or_product = st.text_input("Role or product")
            channel = st.selectbox("Preferred channel", CHANNELS)
        with c2:
            profile_url = st.text_input("X profile / LinkedIn / company URL")
            contact_email = st.text_input("Email address (for email channel)")
            context = st.text_area("Why is this lead relevant? Add public, non-sensitive context.", height=130)
        submitted = st.form_submit_button("Add lead", type="primary")
        if submitted:
            if not company.strip():
                st.error("Company / organization is required.")
            elif channel == "Email" and not contact_email.strip():
                st.warning("No email address entered. You can still save the lead, but email sending won't be available until you add one.")
                lead_id, err = add_lead({
                    "kind":kind,"company":company,"person":person,"role_or_product":role_or_product,
                    "channel":channel,"profile_url":profile_url,"contact_email":contact_email,
                    "context":context
                })
                if err: st.warning(err)
                else: st.success(f"Lead #{lead_id} added.")
            else:
                lead_id, err = add_lead({
                    "kind":kind,"company":company,"person":person,"role_or_product":role_or_product,
                    "channel":channel,"profile_url":profile_url,"contact_email":contact_email,
                    "context":context
                })
                if err: st.warning(err)
                else: st.success(f"Lead #{lead_id} added.")

    st.divider()
    st.subheader("Lead pipeline")
    filter_kind = st.selectbox("Filter pipeline", ["All"] + KINDS)
    leads = fetch_leads(filter_kind)
    if not leads:
        st.info("No leads yet. Add one above or import a CSV in the Import / export tab.")
    else:
        for lead in leads:
            with st.expander(f"#{lead['id']} · {lead['kind']} · {lead['company']} · {lead['status']}"):
                a, b = st.columns([2,1])
                with a:
                    st.write(f"**Contact:** {lead['person'] or '—'}")
                    st.write(f"**Role/product:** {lead['role_or_product'] or '—'}")
                    st.write(f"**Channel:** {lead['channel']}")
                    if lead["profile_url"]:
                        st.write(f"**Profile / URL:** {lead['profile_url']}")
                    if lead["contact_email"]:
                        st.write(f"**Email:** {lead['contact_email']}")
                    st.write(f"**Context:** {lead['context'] or '—'}")
                    st.markdown("**Profile research & build ideas**")
                    research_text = st.text_area(
                        "Research brief (editable; used when drafting)", value=lead["research"] or "",
                        height=260, key=f"res_{lead['id']}", label_visibility="collapsed",
                        placeholder="Click 'Research profile & ideas' to have Claude look up this lead and suggest what you could build for them.",
                    )
                    if st.button("Save research edits", key=f"ress_{lead['id']}"):
                        update_lead(lead["id"], research=research_text)
                        st.success("Research saved.")
                with b:
                    new_status = st.selectbox("Status", STATUSES, index=STATUSES.index(lead["status"]) if lead["status"] in STATUSES else 0, key=f"ls_{lead['id']}")
                    if st.button("Save status", key=f"lss_{lead['id']}"):
                        update_lead(lead["id"], status=new_status)
                        st.success("Status updated. Refresh the page to see changes.")
                    if st.button("Research profile & ideas", key=f"research_{lead['id']}"):
                        if not api_key:
                            st.error("Add your Anthropic API key in the sidebar.")
                        else:
                            with st.spinner("Claude is researching this lead on the web…"):
                                try:
                                    run_research(lead, profile, api_key, model)
                                    st.session_state.pop(f"res_{lead['id']}", None)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Research failed: {e}")
                    if st.button("Generate draft for this lead", key=f"gen_{lead['id']}", type="primary"):
                        if not api_key:
                            st.error("Add your Anthropic API key in the sidebar.")
                        else:
                            attach = lead["kind"] == "Job" and lead["channel"] == "Email" and bool(get_resume())
                            lead_data = dict(lead)
                            try:
                                if not (lead_data.get("research") or "").strip():
                                    with st.spinner("Researching this profile first…"):
                                        lead_data["research"] = run_research(lead_data, profile, api_key, model)
                                    st.session_state.pop(f"res_{lead['id']}", None)
                                content = resume_blocks() + [{"type": "text", "text": draft_prompt(lead_data, profile, attach)}]
                                with st.spinner("Writing draft…"):
                                    parsed = parse_json(call_claude(content, api_key, model, json_schema=DRAFT_SCHEMA))
                                draft_id = save_draft(lead["id"], parsed.get("subject", ""), parsed.get("body", ""))
                                st.success(f"Draft #{draft_id} created. Open the Drafts & sending tab to review it.")
                            except Exception as e:
                                st.error(f"Draft generation failed: {e}")

with draft_tab:
    st.subheader("Review, approve, and send")
    st.warning("Clicking Approve & send on an email draft sends it immediately to the address shown, so check the recipient and message first. X messages are copied and sent manually.")
    drafts = fetch_drafts()
    if not drafts:
        st.info("No drafts yet. Generate a draft from a lead.")
    else:
        for draft in drafts:
            label = f"Draft #{draft['id']} · {draft['kind']} · {draft['company']} · {'Sent' if draft['sent_at'] else ('Approved' if draft['approved'] else 'Needs review')}"
            with st.expander(label, expanded=not bool(draft["approved"]) and not bool(draft["sent_at"])):
                st.write(f"**Recipient/contact:** {draft['person'] or '—'}")
                st.write(f"**Channel:** {draft['channel']}")
                if draft["channel"] == "Email":
                    st.write(f"**To:** {draft['contact_email'] or 'Missing email'}")
                if draft["profile_url"]:
                    st.write(f"**Profile:** {draft['profile_url']}")
                subject_key = f"sub_{draft['id']}"
                body_key = f"body_{draft['id']}"
                subj = st.text_input("Subject", value=draft["subject"], key=subject_key) if draft["channel"] == "Email" else ""
                body = st.text_area("Message", value=draft["body"], height=180, key=body_key)
                is_email = draft["channel"] == "Email"
                saved_resume = get_resume() if is_email else None
                attach_resume = False
                if is_email and saved_resume and not draft["sent_at"]:
                    attach_resume = st.checkbox(
                        f"Attach my resume ({saved_resume[0].upper()})",
                        value=draft["kind"] == "Job", key=f"attach_{draft['id']}",
                    )

                def send_now():
                    attachment_name = None
                    if attach_resume:
                        attachment_name = f"{(profile_name.strip() or 'My').replace(' ', '_')}_Resume.{saved_resume[0]}"
                    try:
                        send_email_smtp(
                            draft["contact_email"], subj, body,
                            attachment=saved_resume if attach_resume else None,
                            attachment_name=attachment_name,
                        )
                        mark_sent(draft["id"], draft["lead_id"])
                        st.success(f"Approved and sent to {draft['contact_email']}.")
                    except Exception as e:
                        st.error(f"Approved, but the email was not sent: {e}")

                c1, c2 = st.columns(2)
                with c1:
                    if not draft["sent_at"] and st.button("Save edits", key=f"save_{draft['id']}"):
                        set_draft(draft["id"], subject=subj, body=body)
                        st.success("Draft saved.")
                with c2:
                    if draft["sent_at"]:
                        st.success(f"Sent at {draft['sent_at']}.")
                    elif is_email and not draft["contact_email"]:
                        st.error("Add a recipient email to this lead before approving.")
                    elif is_email:
                        label = "Retry send" if draft["approved"] else f"Approve & send to {draft['contact_email']}"
                        if st.button(label, key=f"approve_{draft['id']}", type="primary"):
                            set_draft(draft["id"], subject=subj, body=body, approved=1)
                            update_lead(draft["lead_id"], status="Approved")
                            send_now()
                    elif not draft["approved"]:
                        if st.button("Approve this draft", key=f"approve_{draft['id']}", type="primary"):
                            set_draft(draft["id"], subject=subj, body=body, approved=1)
                            update_lead(draft["lead_id"], status="Approved")
                            st.success("Approved. Copy the message below and send it yourself.")
                    else:
                        st.success("Approved by you")
                if draft["channel"] == "X DM (manual)":
                    st.info("X DM is manual only: use the copy icon on the message below, open the profile, verify the recipient, and paste. This app does not automate X DMs.")
                    st.code(body, language=None, wrap_lines=True)
                    if draft["profile_url"]:
                        st.link_button("Open profile to send DM", draft["profile_url"])
                    if draft["approved"] and not draft["sent_at"]:
                        if st.checkbox("I sent this approved X DM manually", key=f"manualsent_{draft['id']}"):
                            if st.button("Mark as sent", key=f"markx_{draft['id']}"):
                                mark_sent(draft["id"], draft["lead_id"])
                                st.success("Marked as sent.")

with data_tab:
    st.subheader("Import leads")
    st.download_button("Download CSV template", data=csv_template(), file_name="outreach_leads_template.csv", mime="text/csv")
    uploaded = st.file_uploader("Upload leads CSV", type=["csv"])
    if uploaded is not None:
        try:
            content = uploaded.getvalue().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(content))
            st.write("Preview", list(reader)[:5])
            if st.button("Import CSV leads", type="primary"):
                reader = csv.DictReader(io.StringIO(content))
                added, skipped = 0, 0
                errors = []
                for row in reader:
                    kind = (row.get("kind") or "Job").strip()
                    channel = (row.get("channel") or "X DM (manual)").strip()
                    if kind not in KINDS: kind = "Job"
                    if channel not in CHANNELS: channel = "X DM (manual)"
                    if not (row.get("company") or "").strip():
                        skipped += 1
                        continue
                    lead_id, err = add_lead({
                        "kind":kind, "company":row.get("company",""),
                        "person":row.get("person",""), "role_or_product":row.get("role_or_product",""),
                        "channel":channel, "profile_url":row.get("profile_url",""),
                        "contact_email":row.get("contact_email",""), "context":row.get("context","")
                    })
                    if err:
                        skipped += 1
                        errors.append(f"{row.get('company','')}: {err}")
                    else:
                        added += 1
                st.success(f"Imported {added}; skipped {skipped}.")
                if errors:
                    st.write(errors[:20])
        except Exception as e:
            st.error(f"Could not read CSV: {e}")

    st.divider()
    st.subheader("Export current lead pipeline")
    leads = fetch_leads()
    output = io.StringIO()
    fields = ["id","kind","company","person","role_or_product","channel","profile_url","contact_email","context","status","created_at","updated_at"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in leads:
        writer.writerow({k: row[k] for k in fields})
    st.download_button("Export leads CSV", data=output.getvalue(), file_name="outreach_leads_export.csv", mime="text/csv")

    st.divider()
    st.subheader("Full backup (leads, drafts, research, profile, resume)")
    st.caption("Streamlit Cloud wipes outreach.db whenever the app restarts or sleeps. Download a backup after each session and restore it when you come back.")
    connect_db().close()
    with open(DB_PATH, "rb") as f:
        st.download_button("Download backup", data=f.read(), file_name=f"outreach_backup_{datetime.date.today()}.db", mime="application/octet-stream")
    backup = st.file_uploader("Restore from backup", type=["db"])
    if backup is not None:
        data = backup.getvalue()
        if not data.startswith(b"SQLite format 3\x00"):
            st.error("That file is not an outreach backup.")
        elif st.checkbox("Replace everything in the app with this backup"):
            if st.button("Restore backup", type="primary"):
                with open(DB_PATH, "wb") as f:
                    f.write(data)
                connect_db().close()
                st.success("Backup restored.")
                st.rerun()
