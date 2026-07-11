# Gmail classification taxonomy

The Gmail Stage-1 prefilter uses generic buckets to explain why a whitelisted email was promoted to the LLM. The editable rules live in `gmail_keyword_roots.txt` as `bucket | root` lines.

- `deadline`: a date or cutoff the user must meet.
- `payment`: money, fees, or invoices requiring action.
- `document`: forms, consent, signatures, or requested documents.
- `response`: confirmations, registrations, RSVPs, or required replies.
- `schedule`: appointments or material schedule changes.
- `preparation`: something to bring, prepare, complete, or submit.
- `attention`: explicit urgency or action-required language.

Keep this taxonomy generic. Never add mailbox addresses, personal names, organizations, or copied email content.
