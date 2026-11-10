-- Seed data. PLACEHOLDERS: replace URLs and Render service ids once the owner confirms them
-- (Section 12, question 1). Tokens are NOT seeded here; use scripts/new_project_token.py.

insert into public.owners(email) values ('OWNER_EMAIL@example.com') on conflict do nothing;

insert into public.projects (id, name, description, live_url, repo_url, tags, sort_order) values
  ('recruit-ai', 'Recruit AI',
   'Uploads a resume, scores it with an LLM, and runs an automated voice interview for strong candidates.',
   'https://REPLACE-ME.onrender.com', 'https://github.com/REPLACE-ME/recruit-ai',
   array['agents','voice','fastapi','llm'], 10)
on conflict (id) do nothing;

-- Config row is created automatically with safe defaults (llm_mode=free, voice off, enabled=false).
update public.project_config
   set extra = jsonb_build_object('score_threshold', 70)
 where project_id = 'recruit-ai';

insert into public.project_services (id, project_id, label) values
  ('srv-REPLACE-BACKEND', 'recruit-ai', 'backend')
on conflict (id) do nothing;
