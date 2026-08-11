# Phase 3 smoke checklist (live Twilio test credentials)

Needs: saas deployment with ARQ worker, platform Twilio account (trial ok for
US), org with ledger balance >= rental.

- [ ] /phone-numbers shows pricing; search US by area code returns numbers.
- [ ] Buy a number: subaccount voxagent-org-{id} appears in Twilio console,
      number purchased inside it, VoiceUrl = .../api/v1/telephony/inbound/run.
- [ ] Ledger shows number_rental debit (minutes-equivalent) immediately.
- [ ] Assign an inbound agent; call the number from a phone -> agent answers
      (inbound resolver matches subaccount sid + called number).
- [ ] Post-call ledger shows AI debit + carrier_debit for the platform call.
- [ ] BYO-number call shows NO carrier_debit.
- [ ] Number appears as an outbound caller-id option (from_numbers pool).
- [ ] Platform config/number invisible to generic telephony edit/delete (403).
- [ ] Drain balance below rental, run charge_number_rentals (trigger via
      the ARQ worker locally or wait for the 02:00 UTC cron) ->
      rental_status past_due + banner on /phone-numbers.
- [ ] Top up -> next run charges and returns to active.
- [ ] Set next_renewal_at 8 days back with low balance -> run releases the
      number at Twilio and marks released.
- [ ] Release flow from UI removes the number at Twilio.
- [ ] Superadmin GET /superuser/platform-numbers lists all numbers.
- [ ] OSS mode: /phone-numbers redirects; routes 404.
