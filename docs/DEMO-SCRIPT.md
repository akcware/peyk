# Demo video script (≤ 5 min)

Sahne yönergeleri Türkçe, söylenecekler İngilizce (jüri için). Ekranda: sol yarı Telegram (telefon ya da masaüstü), sağ yarı terminal (worker logları) — logların JSON olması iyi görünür; `LOG_LEVEL=INFO`.
Hazırlık: worker çalışıyor, kota temiz (`make requeue-failed` değil; gerekiyorsa `sent_notification`'ı o güne ait temizle), takvime 18 dk sonrasına "Client standup" koy, bir arkadaş ikinci Telegram hesabıyla hazır beklesin.

## 0:00–0:40 Problem
[Ekran: gerçek Gmail inbox, 60+ okunmamış; ya da landing page'deki 60→4 animasyonu]
"Sixty things land in my inbox every day, and every one of them screams equally. Newsletters, receipts, and the one mail from a client whose checkout is down. Notifications don't know the difference. So I built an agent that does."

## 0:40–1:10 What it is
[Ekran: landing page hero]
"Proactive Agent watches your mail and calendar, decides what's actually urgent, and interrupts you only within a daily budget. It talks like a secretary, drafts replies you approve, and never sends anything on its own. Built with the Strands Agents SDK on Amazon Bedrock."

## 1:10–2:10 Live: triage + budget
[Kendinize iki mail atın: biri "Weekly digest" tarzı, biri "Invoice #2041 due Friday — please confirm today" (başka bir hesaptan ya da kendinize)]
"Two mails just arrived. Watch the worker: both are triaged by Haiku in about two seconds. The newsletter gets urgency 1 — silent. The invoice gets 4 — and here it is in Telegram, summarized in my language, with useful / noise / mute buttons."
[Loglarda `triage.decided ... notify=false below_threshold` ve `notify=true` satırlarını göster]
"The model decides importance. A pure function decides whether it's worth interrupting me: quota, cooldown, quiet hours, and a reserve kept for real emergencies."

## 2:10–3:10 Live: secretary chat + draft approval
[Telegram: "who mailed me today?"]
"Ask it something. First reflex: 'Sure, checking today's mail…' — then the answer, from real observations."
[Telegram: "write to Mara that we move the meeting to tomorrow" → taslak → Düzenle → "make it 3 pm" → yeni taslak → eski mesajdaki Gönder'e bas → red → yeni Gönder → ✅]
"It drafts, I edit, and here's the part I care about: the old Send button carries the old content hash. It can't send the edited draft. Only the newest version goes out — to Gmail, through Composio."

## 3:10–3:50 Live: onboarding by conversation + calendar
[Arkadaşın telefonu: "hi" → ajan kendini tanıtır, soru sorar → "yes, connect gmail" → link → tıkla → "✅ Gmail connected"]
"Anyone can use it. No commands: it introduces itself, asks one question, sends the login link, and starts watching."
[Takvim bildirimi 15 dk kala gelirse göster]
"Calendar was added with one mapping row and zero changes to the core — there's a test that proves it."

## 3:50–4:30 Architecture
[docs/ARCHITECTURE.md diyagramı]
"One queue in Postgres, adapters for sources, a deterministic gate, and the agent package that never touches the database — so it runs isolated on Bedrock AgentCore. Composio does auth and triggers; a reconcile job catches what polling misses, because we measured that a disabled trigger loses events."

## 4:30–5:00 Close
"Sixty a day down to four. Drafts you approve. An assistant that behaves like a good secretary, not a firehose. Code is MIT on GitHub."
[Ekran: repo + landing page URL]
