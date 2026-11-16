# SMM system

CrewAI plans the week; Python does the numbers, the approval gate and the publishing.

```bash
cd smm-system
pip install -r requirements.txt && pip freeze > requirements.lock
cp .env.example .env   # add OPENAI_API_KEY; leave SMM_PUBLISH_MODE=dry_run

python -m smm.cli selftest                      # no keys, no network, throwaway DB
python -m smm.cli plan --client client.json
python -m smm.cli approve --run-id <RID> --all --approver alice
python -m smm.cli publish --run-id <RID>        # dry run
SMM_PUBLISH_MODE=live python -m smm.cli publish --run-id <RID> --live

# Weekly loop, Mondays 07:00 UTC:
# 0 7 * * 1 cd /srv/smm-system && python -m smm.cli weekly --metrics data/last_7d.csv >> logs/weekly.log 2>&1
```

Publish states: `published`, `dry_run_ok`, `blocked` (lint or no approval for this exact hash),
`manual_required` (no adapter for the platform or format), `not_due`, `throttled`,
`failed_retryable` (provably never reached the platform; the next run retries),
`failed_fatal`, and `unknown` (may have posted: check the platform by hand, never re-send).

Live adapters: Instagram Image/Infographic/Reel, X text, LinkedIn text (`ugcPosts`).
Verify endpoints and API versions against current docs before going live.
