# Metasync Metadata Platform

Metasync is a prototype metadata workspace for universities and research institutions. Members of an invited institutional workspace can upload CSV, TSV, and Excel reports, clean and review records, and download exports. Pandas performs conservative field-based normalization, while SQLite keeps the source rows and cleaned records together.

## Copyright and credits

Copyright © 2026 Denis Kinoti. All rights reserved for the application source code, backend, and software implementation in this repository. This software ownership notice does not claim ownership of the wireframe contribution by Joy Kiama or of third-party packages and assets, which remain subject to their respective terms. No license to reproduce, distribute, modify, or use Denis Kinoti’s software contributions is granted without prior written permission.

Wireframe development: Joy Kiama.

The interface includes a light/dark theme control in the top bar. Your selection is saved in this browser.

## Run the local prototype

Install Python 3.10 or newer, then double-click `start.bat`. The script creates a virtual environment under `%LOCALAPPDATA%\Metasync\venv` on first run, installs `requirements.txt`, starts the API, and opens `http://127.0.0.1:8000`.

The first person to register creates an institutional workspace and becomes its administrator. In **Settings → Account**, a signed-in user can change their sign-in email by entering their current password and the new address; the same account, workspace membership, and administrator role are retained. Email changes take effect immediately and this local prototype does not send a verification email. In **Settings**, the admin can create a single-use invitation code that expires after three days. A colleague from another institution can use that code when creating an account to join the same shared workspace. Their institution name is attached to their uploads. Users in the same workspace can see, clean, review, and export all workspace reports; data from other workspaces is isolated.

The service currently binds to the local computer. Universities on other networks cannot sign in until the app is deployed to a shared server behind HTTPS. Do not expose the development server directly to the public internet.

## Supported workflow

- Upload CSV, tab-separated TSV, and Excel `.xlsx` reports, up to 25 MB and 50,000 rows per report.
- Keep original rows and cleaned values in the local SQLite repository.
- Search/filter report rows and shared catalog records through the API.
- Apply pandas cleanup rules for whitespace, common author initials, language labels, selected subject aliases, publication dates, and identifiers.
- Detect repeated files by SHA-256 within a workspace, find exact duplicate rows, and flag likely duplicate bibliographic records for a person to review.
- Merge a reviewed duplicate while retaining the source record, or ignore the candidate pair.
- Download workspace records as CSV, JSON, or a basic MARC21 mapping.

`sample_metadata.csv` is synthetic demonstration data. Upload it to try cleanup and duplicate review; it contains no real institutional or personal records.

## Account and API routes

- `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, and `GET /api/auth/me` manage accounts and signed-in sessions. `POST /api/auth/change-email` changes the signed-in user email after confirming their current password.
- `POST /api/auth/invites` creates a one-time workspace invitation for an administrator.
- `POST /api/reports` accepts a report with the signed-in member’s institution attribution.
- `GET /api/reports`, `GET /api/reports/{report_id}`, and `GET /api/reports/{report_id}/rows` list reports and filter rows.
- `POST /api/reports/{report_id}/clean` applies enabled pandas cleanup rules.
- `GET /api/records` filters the current workspace’s catalog.
- `GET /api/duplicates` finds possible duplicates; `POST /api/duplicates/{pair_id}/resolve` merges or ignores a pair.
- `GET /api/export?format=csv|json|marc` downloads catalog data.
- `GET /api/dashboard` summarizes workspace activity by institution; `GET /api/institutions` and `POST /api/institutions` list and register report sources; `POST /api/workspace/reset` clears repository data for an administrator; `GET /api/health` reports service availability.
- Interactive API documentation is available at `http://127.0.0.1:8000/docs` while the local service is running.

All data routes require an authenticated session and are scoped to the signed-in user’s workspace. Passwords are stored as salted PBKDF2 hashes; the session cookie is HTTP-only and uses SameSite=Lax. Direct HTTP clients can call the login endpoint and reuse the returned session cookie; machine-to-machine API keys are not implemented yet.

## MVP boundaries and deployment needs

This is a controlled demonstration prototype, not a production consortium service. It does not yet include password reset, login rate limiting, a detailed audit trail, automated backups, institution-level approval workflows, or per-report privacy settings. Payment collection, premium entitlement, and premium catalogue downloads are not implemented in this prototype. Every member of a workspace can access all workspace data. Cleanup and similarity rules are heuristic and require librarian review. MARC files are not accepted as input, MARC21 export is a basic field mapping, and the authority-control screen is illustrative rather than linked to an external authority source.

Before remote or production use, deploy behind HTTPS, set up operational backups and retention, add rate limiting and account recovery, establish workspace administration and data-sharing agreements, and complete functional and usability evaluations. The repository does not yet contain those evaluation results.

The SQLite database and uploaded source reports are stored under `%LOCALAPPDATA%\Metasync\data`, outside the OneDrive-synced project folder.



## Test reports

Two synthetic CSV examples for cleanup and duplicate review are in the test-data folder. See that folder's README for notes and an upload order. The examples contain fictional records only.

## Institution tracking, sharing, and workspace reset

Workspace members can register submitting institutions in **Upload Records**, then assign each report to one registered source. The dashboard compares reports, active records, and harmonization progress by institution. The administrator can reset repository data in **Settings** after entering their password and typing `RESET`; report files, records, duplicate decisions, extra institution registrations, and invites are removed, while workspace identity and member accounts remain.

The Export page can use the browser's native share sheet to share a clean CSV file with compatible apps. The Authority Control area lists premium catalogue and analysis services as locked. This prototype does not collect payments or activate premium access; a payment provider and verified activation workflow must be configured before charging users.