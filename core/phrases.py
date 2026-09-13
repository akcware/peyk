"""Fixed texts the workers send without the agent (approval cards, buttons, feedback acks, fallbacks), in the
person's language. Keep them short and spoken — they sit in the same chat as the agent's own messages, so they
must not read like system output (no ids, no hashes, no "Sent (id …)")."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "draft": "Draft",
        "draft_reply": "Draft (reply in thread)",
        "draft_doc": "Draft — {label}",
        "to": "To: {to}",
        "subject": "Subject: {subject}",
        "title": "Title: {title}",
        "parent": "In: {parent}",
        "btn_send": "✅ Send",
        "btn_create": "✅ Create",
        "btn_edit": "✏️ Edit",
        "btn_cancel": "❌ Cancel",
        "sent": "Sent 👍",
        "send_failed": "Couldn't send it. Tap Send and I'll try again.",
        "send_failed_thread": "I can't find that mail thread in your inbox, so the reply has nowhere to go. "
                              "Want it as a new mail instead? Just tell me.",
        "send_failed_auth": "Couldn't send it, the mail connection needs a fresh sign-in. Tell me and I'll send you the link.",
        "cancelled": "Cancelled, nothing was sent.",
        "edit_prompt": "Okay, send me the corrected text as your next message.",
        "no_draft": "There's no draft open right now.",
        "draft_changed": "The draft changed, approve the newest one",
        "nothing_to_edit": "Nothing to edit",
        "ack_sent": "Sent",
        "ack_send_failed": "Couldn't send",
        "ack_cancelled": "Cancelled",
        "ack_waiting_edit": "Waiting for your correction",
        "doc_created": "📄 {title}\n{url}",
        "doc_created_nolink": "📄 {title} is ready.",
        "doc_failed": "I couldn't create the document, I'll try again a bit later.",
        "fb_useful": "👍 useful",
        "fb_noise": "👎 noise",
        "fb_mute": "🔇 mute thread",
        "noted_useful": "Noted, thanks 👍",
        "noted_noise": "Got it, less of that 👎",
        "unknown_notification": "I don't know that notification anymore",
        "nothing_to_mute": "Nothing to mute",
        "muted": "Muted that thread 🔇",
        "brief_quiet": "Good morning ☀️ Nothing important came in since yesterday. Enjoy the quiet.",
        "brief_header": "Good morning ☀️ Since yesterday:",
        "remind_set": "⏰ I'll remind you {when}: {note}",
        "stuck": "I got stuck on that one, could you ask again?",
        "cal_new": "📅 New event",
        "cal_change": "📅 Change",
        "cal_delete": "📅 Delete event",
        "cal_rsvp": "📅 Reply to invite",
        "cal_guests": "Guests: {guests}",
        "cal_where": "Where: {where}",
        "cal_was": "Was: {when}",
        "cal_meet": "With a Google Meet link",
        "cal_notify": "Guests get notified.",
        "cal_answer": "Answer: {answer}",
        "rsvp_accepted": "I'll be there",
        "rsvp_declined": "Can't make it",
        "rsvp_tentative": "Maybe",
        "btn_cal_create": "✅ Add",
        "btn_cal_update": "✅ Change",
        "btn_cal_delete": "✅ Delete",
        "btn_cal_rsvp": "✅ Send",
        "cal_done_create": "Added to your calendar 👍",
        "cal_done_update": "Changed it 👍",
        "cal_done_delete": "Deleted it from your calendar.",
        "cal_done_rsvp": "Sent your answer 👍",
        "cal_added": "📅 {title}, {when}\n{url}",
        "cal_added_nolink": "📅 {title}, {when}",
        "cal_cancelled": "Okay, I left your calendar as it is.",
        "ack_cal_done": "Done",
        "cal_failed": "I couldn't write to your calendar. Tap again and I'll retry.",
        "cal_failed_missing": "I can't find that event anymore, it may have been deleted.",
        "cal_failed_auth": "The calendar connection needs a fresh sign-in. Tell me and I'll send you the link.",
        "cal_direct_failed": "I couldn't put it in your calendar, could you ask again in a moment?",
        "all_day": "all day",
        "brief_nothing_new": "Good morning ☀️ Nothing important came in since yesterday.",
        "brief_agenda": "Today in your calendar:",
    },
    "tr": {
        "draft": "Taslak",
        "draft_reply": "Taslak (aynı konuya yanıt)",
        "draft_doc": "Taslak — {label}",
        "to": "Kime: {to}",
        "subject": "Konu: {subject}",
        "title": "Başlık: {title}",
        "parent": "Nerede: {parent}",
        "btn_send": "✅ Gönder",
        "btn_create": "✅ Oluştur",
        "btn_edit": "✏️ Düzelt",
        "btn_cancel": "❌ Vazgeç",
        "sent": "Gönderdim 👍",
        "send_failed": "Gönderemedim. Gönder'e bir daha basarsan tekrar denerim.",
        "send_failed_thread": "Bu mail zincirini gelen kutunda bulamadım, cevabın gidecek yeri yok. "
                              "İstersen yeni mail olarak göndereyim, söylemen yeter.",
        "send_failed_auth": "Gönderemedim, mail bağlantısının yenilenmesi gerekiyor. Söyle, bağlantı linkini yollayayım.",
        "cancelled": "Vazgeçtim, bir şey gönderilmedi.",
        "edit_prompt": "Tamam, düzeltilmiş halini yaz, onu göndereyim.",
        "no_draft": "Şu an açık bir taslak yok.",
        "draft_changed": "Taslak değişti, en yenisini onayla",
        "nothing_to_edit": "Düzeltilecek bir şey yok",
        "ack_sent": "Gönderildi",
        "ack_send_failed": "Gönderilemedi",
        "ack_cancelled": "Vazgeçildi",
        "ack_waiting_edit": "Düzeltmeni bekliyorum",
        "doc_created": "📄 {title}\n{url}",
        "doc_created_nolink": "📄 {title} hazır.",
        "doc_failed": "Belgeyi oluşturamadım, biraz sonra tekrar deneyeyim.",
        "fb_useful": "👍 işe yaradı",
        "fb_noise": "👎 gereksiz",
        "fb_mute": "🔇 bu konuyu sustur",
        "noted_useful": "Not aldım, sağ ol 👍",
        "noted_noise": "Tamam, bunlardan daha az 👎",
        "unknown_notification": "Bu bildirimi artık bulamıyorum",
        "nothing_to_mute": "Susturacak bir şey yok",
        "muted": "Bu konuyu susturdum 🔇",
        "brief_quiet": "Günaydın ☀️ Dünden beri önemli bir şey gelmedi. Sakin bir gün.",
        "brief_header": "Günaydın ☀️ Dünden beri:",
        "remind_set": "⏰ {when} hatırlatırım: {note}",
        "stuck": "Bu sefer takıldım, bir daha sorar mısın?",
        "cal_new": "📅 Yeni etkinlik",
        "cal_change": "📅 Değişiklik",
        "cal_delete": "📅 Etkinliği sil",
        "cal_rsvp": "📅 Davete cevap",
        "cal_guests": "Davetliler: {guests}",
        "cal_where": "Yer: {where}",
        "cal_was": "Önceki: {when}",
        "cal_meet": "Google Meet linkiyle",
        "cal_notify": "Davetlilere bildirim gider.",
        "cal_answer": "Cevap: {answer}",
        "rsvp_accepted": "Katılıyorum",
        "rsvp_declined": "Katılamıyorum",
        "rsvp_tentative": "Belki",
        "btn_cal_create": "✅ Ekle",
        "btn_cal_update": "✅ Değiştir",
        "btn_cal_delete": "✅ Sil",
        "btn_cal_rsvp": "✅ Gönder",
        "cal_done_create": "Takvime ekledim 👍",
        "cal_done_update": "Değiştirdim 👍",
        "cal_done_delete": "Takvimden sildim.",
        "cal_done_rsvp": "Cevabını gönderdim 👍",
        "cal_added": "📅 {title}, {when}\n{url}",
        "cal_added_nolink": "📅 {title}, {when}",
        "cal_cancelled": "Tamam, takvime dokunmadım.",
        "ack_cal_done": "Tamam",
        "cal_failed": "Takvime yazamadım. Bir daha basarsan tekrar denerim.",
        "cal_failed_missing": "Bu etkinliği artık bulamıyorum, silinmiş olabilir.",
        "cal_failed_auth": "Takvim bağlantısının yenilenmesi gerekiyor. Söyle, linki yollayayım.",
        "cal_direct_failed": "Takvime ekleyemedim, birazdan tekrar söyler misin?",
        "all_day": "tüm gün",
        "brief_nothing_new": "Günaydın ☀️ Dünden beri önemli bir şey gelmedi.",
        "brief_agenda": "Bugün takviminde:",
    },
    "de": {
        "draft": "Entwurf",
        "draft_reply": "Entwurf (Antwort im Thread)",
        "draft_doc": "Entwurf — {label}",
        "to": "An: {to}",
        "subject": "Betreff: {subject}",
        "title": "Titel: {title}",
        "parent": "In: {parent}",
        "btn_send": "✅ Senden",
        "btn_create": "✅ Anlegen",
        "btn_edit": "✏️ Ändern",
        "btn_cancel": "❌ Abbrechen",
        "sent": "Gesendet 👍",
        "send_failed": "Konnte nicht senden. Tipp nochmal auf Senden, dann versuche ich es erneut.",
        "send_failed_thread": "Ich finde diesen Mail-Thread nicht in deinem Postfach, die Antwort hat kein Ziel. "
                              "Sag Bescheid, dann schicke ich sie als neue Mail.",
        "send_failed_auth": "Konnte nicht senden, die Mail-Verbindung muss neu bestätigt werden. Sag Bescheid, dann schicke ich dir den Link.",
        "cancelled": "Abgebrochen, nichts wurde gesendet.",
        "edit_prompt": "Okay, schick mir den korrigierten Text als nächste Nachricht.",
        "no_draft": "Gerade ist kein Entwurf offen.",
        "draft_changed": "Der Entwurf hat sich geändert, bitte den neuesten bestätigen",
        "nothing_to_edit": "Nichts zu ändern",
        "ack_sent": "Gesendet",
        "ack_send_failed": "Senden fehlgeschlagen",
        "ack_cancelled": "Abgebrochen",
        "ack_waiting_edit": "Warte auf deine Korrektur",
        "doc_created": "📄 {title}\n{url}",
        "doc_created_nolink": "📄 {title} ist fertig.",
        "doc_failed": "Ich konnte das Dokument nicht anlegen, ich versuche es später nochmal.",
        "fb_useful": "👍 nützlich",
        "fb_noise": "👎 unnötig",
        "fb_mute": "🔇 Thread stumm",
        "noted_useful": "Notiert, danke 👍",
        "noted_noise": "Verstanden, weniger davon 👎",
        "unknown_notification": "Diese Benachrichtigung finde ich nicht mehr",
        "nothing_to_mute": "Nichts stummzuschalten",
        "muted": "Thread stummgeschaltet 🔇",
        "brief_quiet": "Guten Morgen ☀️ Seit gestern kam nichts Wichtiges. Ein ruhiger Tag.",
        "brief_header": "Guten Morgen ☀️ Seit gestern:",
        "remind_set": "⏰ Ich erinnere dich {when}: {note}",
        "stuck": "Da bin ich hängen geblieben, fragst du nochmal?",
        "cal_new": "📅 Neuer Termin",
        "cal_change": "📅 Änderung",
        "cal_delete": "📅 Termin löschen",
        "cal_rsvp": "📅 Antwort auf Einladung",
        "cal_guests": "Gäste: {guests}",
        "cal_where": "Ort: {where}",
        "cal_was": "Vorher: {when}",
        "cal_meet": "Mit Google-Meet-Link",
        "cal_notify": "Die Gäste werden benachrichtigt.",
        "cal_answer": "Antwort: {answer}",
        "rsvp_accepted": "Ich komme",
        "rsvp_declined": "Ich kann nicht",
        "rsvp_tentative": "Vielleicht",
        "btn_cal_create": "✅ Eintragen",
        "btn_cal_update": "✅ Ändern",
        "btn_cal_delete": "✅ Löschen",
        "btn_cal_rsvp": "✅ Senden",
        "cal_done_create": "Im Kalender eingetragen 👍",
        "cal_done_update": "Geändert 👍",
        "cal_done_delete": "Aus dem Kalender gelöscht.",
        "cal_done_rsvp": "Antwort gesendet 👍",
        "cal_added": "📅 {title}, {when}\n{url}",
        "cal_added_nolink": "📅 {title}, {when}",
        "cal_cancelled": "Okay, dein Kalender bleibt, wie er ist.",
        "ack_cal_done": "Erledigt",
        "cal_failed": "Ich konnte nicht in deinen Kalender schreiben. Tipp nochmal, dann versuche ich es erneut.",
        "cal_failed_missing": "Ich finde diesen Termin nicht mehr, vielleicht wurde er gelöscht.",
        "cal_failed_auth": "Die Kalender-Verbindung muss neu bestätigt werden. Sag Bescheid, dann schicke ich dir den Link.",
        "cal_direct_failed": "Ich konnte es nicht eintragen, fragst du gleich nochmal?",
        "all_day": "ganztägig",
        "brief_nothing_new": "Guten Morgen ☀️ Seit gestern kam nichts Wichtiges.",
        "brief_agenda": "Heute im Kalender:",
    },
}


def phrase(lang: str | None, key: str, **kw: object) -> str:
    """Text `key` in `lang` (ISO 639-1; unknown or empty -> English), with {placeholders} filled in."""
    table = TEXTS.get((lang or "en").split("-")[0].lower(), TEXTS["en"])
    text = table.get(key) or TEXTS["en"][key]
    return text.format(**kw) if kw else text


# ---------- dates, the way a person says them ----------

MONTHS: dict[str, tuple[str, ...]] = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "tr": ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"),
    "de": ("Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sep.", "Okt.", "Nov.", "Dez."),
}
WEEKDAYS: dict[str, tuple[str, ...]] = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "tr": ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"),
    "de": ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."),
}


def _code(lang: str | None) -> str:
    code = (lang or "en").split("-")[0].lower()
    return code if code in MONTHS else "en"


def day_text(d: date, lang: str | None) -> str:
    """'14 Eylül Pazartesi' / 'Mon 14 Sep' / 'Mo. 14. Sep.'"""
    code = _code(lang)
    month, weekday = MONTHS[code][d.month - 1], WEEKDAYS[code][d.weekday()]
    if code == "tr":
        return f"{d.day} {month} {weekday}"
    if code == "de":
        return f"{weekday} {d.day}. {month}"
    return f"{weekday} {d.day} {month}"


def _zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "UTC")
    except Exception:  # noqa: BLE001 - an unknown zone name must not break a card
        return ZoneInfo("UTC")


def _local(iso: str, zone: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(iso)
    return (dt if dt.tzinfo else dt.replace(tzinfo=zone)).astimezone(zone)


def when_text(start: str, end: str | None, tz: str | None, lang: str | None) -> str:
    """An event's time in the person's zone and language: '14 Eylül Pazartesi, 12:00-13:00', 'Mon 14 Sep, all day',
    'Mon 14 Sep 22:00 - Tue 15 Sep 01:00'. All-day events are dates with an exclusive end. Unparseable input
    comes back unchanged."""
    zone = _zone(tz)
    try:
        if len(start) == 10:
            first = date.fromisoformat(start)
            last = date.fromisoformat(end) - timedelta(days=1) if end and len(end) == 10 else first
            if last <= first:
                return f"{day_text(first, lang)}, {phrase(lang, 'all_day')}"
            return f"{day_text(first, lang)} - {day_text(last, lang)}"
        s = _local(start, zone)
        if not end:
            return f"{day_text(s.date(), lang)}, {s:%H:%M}"
        e = _local(end, zone)
        if e.date() == s.date():
            return f"{day_text(s.date(), lang)}, {s:%H:%M}-{e:%H:%M}"
        return f"{day_text(s.date(), lang)} {s:%H:%M} - {day_text(e.date(), lang)} {e:%H:%M}"
    except (TypeError, ValueError):
        return start
