# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/aibix0001/aiblinx/security/advisories/new)
rather than a public issue. You'll get a reply within a week.

## Running aiblinx safely

- The app is **open by default**, meant for a home network or VPN. If it is
  reachable from the internet, set `APP_PASSWORD`. Without it, anyone who can
  reach the page can save to your Linkwarden, import URLs (which makes the
  server fetch them) and change your topics.
- Set `ADMIN_TOKEN`: it protects `POST /admin/run-cycle`, which runs paid model
  calls.
- Put it behind HTTPS (any reverse proxy) when you use the password over the
  internet.
- Your `.env` holds API keys; keep it out of version control (it is in
  `.gitignore`).
