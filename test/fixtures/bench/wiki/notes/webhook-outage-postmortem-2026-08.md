---
type: note
title: Webhook outage postmortem, August 2026
status: active
created: 2026-08-19
updated: 2026-08-26
tags: [postmortem, webhook, incident]
aliases: [august outage]
---
# Webhook outage postmortem, August 2026

Blameless writeup of the August incident on [[webhook-relay]], in which a share of customer callbacks failed for about three hours. Written the week after, while the details were still fresh.

## Summary
Between 09:40 and 12:55 UTC roughly a third of inbound webhook deliveries were rejected with a 401 by the relay, and the senders' retry schedules stretched the visible impact to the afternoon. No events were lost, because rejected deliveries are retried by the sender and the relay journals everything it accepts. The first customer report arrived at 10:20; the alert that should have fired earlier was muted during a noisy deploy the day before and never un-muted.

## Timeline
At 09:40 the first rejections appear in the logs. At 10:20 a customer writes in. At 10:35 I confirm the pattern: failures are clustered on one of the two receiver hosts. At 11:10 the failing host is taken out of rotation, which halves the error rate but does not end it, because the retries keep landing on the remaining host. At 12:05 the actual cause is found and at 12:55 the fix is rolled out and the backlog drains.

## Impact
About 4,100 deliveries were rejected at least once; 38 customers noticed. The longest delay for any single event was five hours and ten minutes. Support load was one afternoon of replies. There was no data loss and no incorrect data was delivered, which is the property the journal exists to guarantee.

## Detection
The signature-failure alert has a threshold of five percent over ten minutes, and it would have fired within fifteen minutes of the first rejection. It was muted the day before, during a deploy that briefly produced failures on purpose, and the mute had no expiry. The only other signal was the delivery success graph on the dashboard, which I do not watch continuously. In short, the incident was detected by a customer, not by monitoring, and that is the first thing to fix.
## Contributing factors
The deploy the previous day muted the signature-failure alert and nobody un-muted it. The two receiver hosts are configured by hand and had drifted apart. The status page was updated only after the first customer message. None of these alone would have caused a three-hour incident; together they did.

## What went well
The journal did its job: nothing accepted was lost and every rejected delivery was eventually retried by its sender. Rolling the fix out one host at a time worked as designed. Support replies went out the same day, using the template from the previous incident, and customers who wrote in got a personal line rather than only the template. The decision to write the timeline while the incident was still open meant the minutes in this document are real timestamps, not memories.
## Follow-up: clock skew on signature checks
The rejections were signature failures, and the signature check includes a timestamp tolerance. The affected receiver's clock had drifted nine minutes ahead after a kernel upgrade silently disabled the time-sync service, so every request looked stale and was refused. We re-enabled chrony on both hosts, added an alert on clock offset above five seconds, and widened the timestamp tolerance from 120 to 300 seconds, which is still well inside the replay-protection window.
