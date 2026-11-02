# Terms of Use

LLMAO, at https://llm.apache.org, is a service of The Apache Software
Foundation's Responsible AI Initiative. By using it you agree to these
terms. They are specific to this service and sit on top of your
Individual Contributor License Agreement with the Foundation and the
[[]Privacy Policy](https://privacy.apache.org/policies/privacy-policy-public.html),
which continue to apply.

## Who may use it, and for what

LLMAO is for official members of Apache Project Management Committees
and ASF committers, and its use is limited to work on ASF codebases.

## What is recorded

Every request sent through the gateway is logged. The log holds the
prompt as the gateway received it (the messages, or the input of an
embeddings request), the parameters sent with it, the model asked for
and the one that served it, the key that was used and its owner and
project, the tool that sent it, the token counts, the timing, and whether
the request succeeded.

Responses are not recorded. What a model produces is not kept.

Logging is a condition of use. No request parameter or header switches
it off; a request that tries is logged like any other.

## Who can read it, and why

The service's site administrators, on the gateway host. No page on this
site shows a prompt. The log exists so that we can understand how the
service is used (which projects, tools and models, what shape the
requests have, how that changes over time, how much capacity to run),
and usage is monitored for non-ASF work, malicious use of AI, and
criminal activity.

## How long it is kept

Approximately 90 days, then it is deleted. The gateway host is covered
by the Foundation's standard backups, which keep copies under their own
retention.

## What to send where

Treat anything you send as visible to the service's administrators.

Models marked **Private** on the [[]Models page](/models) run on
infrastructure the Foundation controls, and a prompt to one of them goes
nowhere else. That is where unpublished security work belongs. Models
marked **External** or **Mixed** send the request to a third-party
provider under that provider's own terms; do not send them embargoed
material.

Whatever the model, do not send credentials, secrets, or personal data
about other people.

## Keys

A key is yours; do not share it. Use is bounded by the allowances shown
on [[]My Keys](/keys), and a request that exceeds them is refused rather
than queued.

## Changes and contact

Development is ongoing. Updates are shared on general@rai.apache.org and
in #llmao-notify on Slack, and the
[[]Responsible AI Initiative](https://rai.apache.org/) site has more.

These terms change when the service does. This page is the version
running now, from
[[]commit [commit]]([repo]/blob/[commit]/docs/help/terms.md); its history
is in the [[]repository]([repo]/commits/main/docs/help/terms.md).
