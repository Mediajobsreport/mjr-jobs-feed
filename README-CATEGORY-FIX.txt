MJR CATEGORY FIX

Replace the existing mjr-ats-crawler.py in the root of the GitHub repository with this version.

Key changes:
- Title/function takes priority over employer type.
- Business Office is checked before Sales & Marketing.
- TV-specific titles are separated from Radio.
- Journalism, Digital, Engineering and PR have stronger title rules.
- Music Industry and Public Media/Higher Ed are fallback employer categories, not automatic overrides.
- Description keywords are only a narrow fallback when the title is ambiguous.

After replacing the file:
1. Commit the change.
2. Actions -> Update MJR Jobs XML -> Run workflow.
3. Re-import/update the master XML in JBoard.
