# ADR-0016: React console served by the API; cookie sessions

- Status: accepted
- Date: 2026-11-12

## Context
Operators need a real work screen (FR-12), the lead needs a dashboard (FR-17). The author has strong front-end experience, and the UI is what people see in the demo.

## Decision
One React + TypeScript single-page app for chat, console and dashboard, built by Vite and served as static files by the API container. Operators log in with email and password; the session is a signed HttpOnly cookie with a CSRF token. Roles are checked on the server.

## Alternatives considered
- **Streamlit or Gradio.** Fast for a prototype; weak for a queue, editing and roles.
- **Separate front-end hosting.** One more deployment and CORS for no gain.
- **OAuth / SSO provider.** Right for a real company; too much for a demo. The auth module is small and can be swapped.
- **JWT in local storage.** Readable by scripts; cookies are safer for a browser app.

## Consequences
One deployable unit, same origin, simple security model. Server rendering and SEO are not needed.
