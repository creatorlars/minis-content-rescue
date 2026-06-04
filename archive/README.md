# archive/ — in-progress site mirrors (gitignored)

This directory holds the working crawl archives. **Everything here except this file is
gitignored** (see repo `.gitignore`) — the mirrors are multi-GB and must never be committed
back to source.

Layout (one subfolder per site, matching `scrapers/common/paths.py`):

```
archive/
  lostminis/                       # Lost Minis Wiki  (www.miniatures-workshop.com/lostminiswiki)
    www.miniatures-workshop.com/   # mirrored pages + assets
    state.json                     # slim resumable crawl state (queue/done/dead)
    assets-index.jsonl             # append-only asset registry
    manifest.yaml  pages-index.yaml  site-tree.yaml  assets-index.yaml
    crawl.log
  solegends/                       # The Stuff of Legends (www.solegends.com)
    www.solegends.com/
    state.json  assets-index.jsonl  manifest.yaml  ...  crawl.log
```

These were relocated from the sibling `web-scrapers/archive/` folder
(`lostminis-test/` and `solegends/`). The legacy `crawl-state.json` (if present) is
migrated into the slim `state.json` on first resume.

## Resume / run

From the repo root:

```
python run.py                 # crawl + validate BOTH sites, block until 100%
python run.py --site solegends --once    # one pass over a single site
python run.py --validate-only            # just report completeness
```
