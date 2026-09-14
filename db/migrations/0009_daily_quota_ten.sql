-- The daily interruption budget is 10 knocks in a rolling 24 h (was 5); the last 2 are still kept for urgency 5.
-- A product decision of 2026-09-14. Everyone still on the old default moves with it; a quota someone set stays.
alter table budget_settings alter column daily_quota set default 10;
update budget_settings set daily_quota = 10 where daily_quota = 5;
