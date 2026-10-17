# AI Outreach Agent v2

A local Streamlit app for managing two private outreach pipelines:
- Job opportunities
- Paid sponsorship leads

Features:
- SQLite lead tracking with duplicate prevention
- CSV lead import/export
- Claude-generated personalized message drafts
- Review/edit/approve workflow
- X DMs remain manual; no automated unsolicited X DMs
- Optional SMTP email sending, only after explicit per-message approval

## Run locally

Python 3.10+ recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate       # macOS/Linux
# .venv\Scripts\activate        # Windows
pip install -r requirements.txt
streamlit run app.py
```

## Configure the LLM

Drafts are generated with the Claude API (default model `claude-opus-5-5`).
Get an Anthropic API key at https://platform.claude.com/settings/keys and enter it in the sidebar.
Or set `ANTHROPIC_API_KEY` and optionally `CLAUDE_MODEL` in your environment.

## Optional email sending via SMTP

Email sending is off until configured. Set these environment variables before launching:

```bash
export SMTP_HOST="smtp.example.com"
export SMTP_PORT="587"
export SMTP_USERNAME="your-account"
export SMTP_PASSWORD="your-app-password"
export SMTP_FROM="you@example.com"
streamlit run app.py
```

Use an app password or provider-approved credential. Never commit secrets to GitHub. Provider-specific SMTP settings vary. Gmail users generally need an app password with 2-Step Verification; do not use your normal account password.

## Workflow

1. Upload your resume in the sidebar (PDF, TXT, or MD). It is saved locally as `my_resume.*` and sent to Claude with research and draft requests.
2. Add leads manually or import a CSV.
3. Add useful, factual context to help personalize the outreach.
4. Click **Research profile & ideas**: Claude searches the web (and fetches the lead's URL) and writes an editable brief covering who they are, what they need, how you fit, and 3-5 things you could build for them.
5. Generate a draft with Claude. It uses your resume and the research brief. Job leads on email are written as short applications, with your resume attached at send time.
6. Review and edit it.
7. Click **Approve this draft**.
8. For X, copy the message, click **Open profile to send DM**, and send it yourself. Mark it sent afterward.
9. For email, choose whether to attach your resume, confirm the exact recipient/message, and click Send approved email after SMTP is configured.

## Privacy and compliance

- This app does not discover or scrape leads automatically; import or add leads you have permission to contact.
- It does not automate X DMs. X messages are copy-and-send manually.
- Send only relevant, lawful outreach; respect opt-outs and platform rules.
- Do not use deceptive claims or fabricate audience analytics, job experience, or relationships.
- Review generated text before approval. LLM prompts include the sender profile and the lead context you provide.
- Lead data and drafts are stored locally in `outreach.db`.
- Your profile and resume are saved in `outreach.db` when you click Save profile or upload a resume.
- SMTP delivery can fail or be rejected by the provider. The app marks an email sent only after SMTP returns success.

## Deploy to Streamlit Community Cloud

1. Push this folder to a **private** GitHub repo (`outreach.db`, your resume, `.env` and `.streamlit/secrets.toml` are git-ignored).
2. Go to https://share.streamlit.io → **Create app** → pick the repo, branch `main`, main file `app.py`.
3. Under **Advanced settings → Secrets**, paste the contents of `.streamlit/secrets.toml.example` with your real values. `APP_PASSWORD` is required: without it the deployed app refuses to load.
4. Deploy. Open the URL, enter your password, upload your resume, and save your profile.

Streamlit Cloud's disk is temporary: the database (leads, drafts, research, profile, resume) is wiped whenever the app restarts or sleeps. Use **Import / export → Download backup** after each session and **Restore from backup** when you return.
