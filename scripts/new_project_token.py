"""Create a PROJECT_TOKEN for a project.

Prints the plaintext token (put it in that project's Render env as PROJECT_TOKEN) and the SQL to
store its hash. Run the SQL in the Supabase SQL editor. The plaintext is never stored anywhere.

    python scripts/new_project_token.py recruit-ai
"""
import hashlib
import secrets
import sys

project_id = sys.argv[1]
token = "pt_" + secrets.token_urlsafe(32)
digest = hashlib.sha256(token.encode()).hexdigest()
print(f"PROJECT_TOKEN={token}\n")
print(f"insert into public.project_tokens (token_sha256, project_id) values ('{digest}', '{project_id}');")
