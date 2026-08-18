# Content policy

Which communities pulpmill is allowed to ingest, and why.

This file is the human-readable half of a rule the code enforces. The machine
half lives in `sources.<name>.blocked_quality_keys` in `config/pipeline.yaml`,
which the ingestion pipeline applies **before** a story is ever persisted.

## The rule

> Do not ingest communities whose content is authored as a creative work by an
> identifiable author who retains and enforces reproduction rights.

## Why this is not "avoid copyrighted content"

Everything posted to Reddit or 4chan is copyrighted the moment it is written.
There is no such thing as an uncopyrighted subreddit, so "avoid copyrighted
subreddits" is not a rule that can be implemented literally. The axis that
actually predicts risk is different, and it has three parts:

| Factor | Low risk | High risk |
|---|---|---|
| Nature of the work | Factual personal anecdote | Original fiction |
| Author | Anonymous, transient account | Named author building a body of work |
| Community norms | Reposting is routine and unremarked | Narration permission is an explicit rule |

A confession posted anonymously and a serialised horror story by an author who
sells collections are treated identically by copyright law and completely
differently by everyone involved in practice. The blocklist below tracks the
practical axis, because that is the one that produces takedowns.

## Blocked

| Community | Reason |
|---|---|
| `r/nosleep` | Original horror fiction. The subreddit's own rules affirm that authors retain all rights, and narration permission is an established, actively enforced norm. Authors do issue takedowns against channels that skip it. |
| `r/LetsNotMeet` | First-person accounts of real encounters involving real, identifiable third parties. The exposure here is as much privacy as copyright. |

Both were ingested during early development. They were removed under this
policy, and stories already stored from them were moved to `REJECTED` rather
than deleted, so the decision stays auditable and reversible.

## Allowed

`r/AmItheAsshole`, `r/TrueOffMyChest`, `r/confession`, `r/relationship_advice`,
`r/MaliciousCompliance`, `r/pettyrevenge`, `r/TalesFromRetail`.

These are anonymous factual anecdotes rather than authored creative works, and
the communities have no narration-permission norm.

4chan `/x/` and `/adv/` are anonymous by construction: there is no author to
attribute to and no rights holder to ask. That removes the copyright question
and leaves ordinary editorial judgement about what is worth publishing.

## What this policy does not cover

**Platform originality rules are a separate problem.** YouTube's monetization
policy on mass-produced and templated content, and Instagram's demotion of
unoriginal reposts, are business risks rather than legal ones. They are not
addressed by this file and are not enforced by the code. Deferred deliberately.

**Attribution is not implemented as consent.** The publishing stage emits a
source link for every video (see `publishing/metadata.py`). Attribution is good
practice and helps a takedown resolve quietly, but it is not permission and
should not be mistaken for it.

## Changing the list

1. Edit `blocked_quality_keys` for the source in `config/pipeline.yaml`.
2. Remove the community from `queries` as well -- the blocklist is a safety net,
   not a substitute for not asking.
3. Run `pulpmill policy --apply` to reject stories already stored from it.
4. Record the reasoning in the table above.

Step 3 is deliberately explicit rather than automatic: rejecting stored stories
is a data change, and it should be a decision rather than a side effect of
editing YAML.
