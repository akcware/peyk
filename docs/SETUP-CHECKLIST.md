# Sizin Tarafınız — Paralel Kurulum Checklist'i

Son teslim: **14 Eylül 2026, 17:00 PDT** (Türkiye saatiyle 15 Eylül 03:00).
Bu dokümandaki her madde kod yazımından bağımsız; sırayla değil, aciliyete göre yapın.
Tamamlananları `[x]` işaretleyin; ben `.env`'e bakarak hangi fazın elle testine geçebileceğimizi anlarım.

---

## A. Bugün — Faz 0 elle testi için (30–45 dk)

### A1. Telegram botu
- [ ] Telegram'da **@BotFather** → `/newbot` → isim ve kullanıcı adı ver → token'ı kopyala.
- [ ] Bota bir kez `/start` yaz (bot sana mesaj atabilsin diye şart).
- [ ] `chat_id`'ni öğren (TOKEN'ı kendi token'ınla değiştir):
  ```bash
  curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -c "import sys,json; [print(u['message']['chat']['id']) for u in json.load(sys.stdin)['result'] if 'message' in u]"
  ```
- [ ] `.env` içine `TELEGRAM_BOT_TOKEN=` ve `TELEGRAM_CHAT_ID=` yaz.

### A2. Composio hesabı ve Gmail bağlantısı
- [ ] https://platform.composio.dev → hesap aç (GitHub ile hızlı).
- [ ] **Settings → API Keys** → key oluştur → `.env`'e `COMPOSIO_API_KEY=`.
- [ ] **Settings → Webhooks** (ya da Project Settings → Event Webhook) → "Webhook secret"i kopyala → `.env`'e `COMPOSIO_WEBHOOK_SECRET=`. Webhook URL'sini şimdilik boş bırak; lokalde WebSocket kullanıyoruz.
- [ ] Dashboard'da **Gmail** toolkit'ine tıkla → auth config oluştur (Composio'nun kendi OAuth uygulaması yeterli, kendi Google Cloud projesi gerekmez) → **Connect** → Google OAuth'u tamamla.
  - Bağlarken kullandığın `user_id` (Composio "entity" / user id) ne ise onu `.env`'e `COMPOSIO_USER_ID=` olarak yaz (dashboard'da varsayılan genelde `default`).
- [ ] Aynı Gmail bağlantısında **Triggers** sekmesi → `GMAIL_NEW_GMAIL_MESSAGE` → Enable. Config: `labelIds=INBOX`, `interval=1` (dakika; en düşük değer neyse onu seç).
- [ ] Kendine bir test maili at → dashboard **Logs / Trigger logs**'ta olay göründüğünde **payload JSON'unu kopyala** ve şuraya kaydet:
  `tests/fixtures/composio_events/gmail_new_message.raw.json`
  Ben bunu anonimleştirip fixture yapacağım; mapping'deki alan adlarını gerçek payload'a göre doğrulamam için bu **kritik**.
- [ ] Gecikmeyi ölç: maili attığın saat → Telegram'a bildirim düştüğü saat. README'ye yazacağız.

### A3. Lokal araçlar
- [ ] Docker Desktop açık kalsın (Postgres compose'da).
- [ ] `.env.example`'ı kopyala: `cp .env.example .env` (ben repo'ya koyacağım).

---

## B. Yarın sabaha kadar — Faz 1 (LLM) için AWS

### B1. AWS Builder ID (hackathon teslimi için zorunlu)
- [ ] https://profiles.aws.amazon.com → Builder ID oluştur. Devpost formuna gerekiyor.

### B2. AWS hesabı + Bedrock erişimi
- [ ] AWS hesabı (yoksa aç; kredi kartı gerekir, Bedrock kullanımı hackathon ölçeğinde birkaç dolar).
- [ ] AWS CLI kur:
  ```bash
  brew install awscli
  ```
- [ ] Credential: en basiti IAM user + access key (`aws configure`), SSO kullanıyorsan `aws sso login --profile <profil>` ve `.env`'e `AWS_PROFILE=<profil>`.
  Gereken izinler: `AmazonBedrockFullAccess` + (AgentCore için) `BedrockAgentCoreFullAccess`, ECR, CodeBuild, IAM PassRole. Hackathon için tek IAM user'a `AdministratorAccess` vermek en hızlısı; teslimden sonra sil.
- [ ] Bölge: **us-east-1** (Claude modelleri + AgentCore Runtime burada kesin var). `.env`: `AWS_REGION=us-east-1`.
- [ ] Bedrock konsolu → **Model access** → Anthropic Claude Haiku ve Sonnet ailesi için erişim iste (genelde anında onaylanır).
- [ ] Model ID'lerini Bedrock konsolu → Model catalog'dan kopyala (cross-region "inference profile" ID'leri `us.` ile başlar). Örnek biçim: `us.anthropic.claude-haiku-4-5-20251001-v1:0`. `.env`:
  `TRIAGE_MODEL_ID=` (Haiku sınıfı), `CHAT_MODEL_ID=` (Sonnet sınıfı).
- [ ] Doğrulama:
  ```bash
  aws bedrock list-foundation-models --region us-east-1 --by-provider anthropic --query 'modelSummaries[].modelId'
  ```
- [ ] **Yedek plan:** Bedrock erişimi gecikirse `ANTHROPIC_API_KEY` ver; kod aynı, `MODEL_PROVIDER=anthropic` ile lokal çalışır. Hackathon şartı Strands kullanmak, Bedrock değil.

### B3. AgentCore (opsiyonel ama puan getiriyor)
- [ ] Bedrock konsolunda **AgentCore** sayfasını bir kez aç (bölgede etkinleştirmiş olursun).
- [ ] Toolkit'i ben `uv` ile kuruyorum; deploy komutlarını (`agentcore configure/launch`) Faz 1 bitince beraber çalıştırırız, senin AWS credential'ın lazım.

---

## C. Faz 2–3 arası — Deploy ve public URL (opsiyonel, "live demo link" bonus)

- [ ] Public webhook için ngrok zaten kurulu:
  ```bash
  ngrok http 8000
  ```
  Çıkan `https://xxxx.ngrok.app` adresini Composio → Webhooks'a `https://xxxx.ngrok.app/webhook/composio` olarak yaz. **Lokalde şart değil**, WebSocket modu public URL istemiyor.
- [ ] Hosted Postgres (pgvector'lı): Neon (https://neon.tech) ücretsiz katman yeterli. `DATABASE_URL`'i al.
- [ ] Workers için basit host: Railway ya da Fly.io (Docker image'ı ben veririm). Fargate daha "AWS'li" görünür ama kurulumu uzun; sadece zaman kalırsa.

---

## D. 13–14 Eylül — Teslim paketi

- [ ] GitHub'da public repo: `gh` ile giriş yapmışsın (akcware), ben `gh repo create` ile açabilirim; repo adını onayla (`proactive-agent`?).
- [ ] Repo **About** kısmında lisansı **MIT** seç (Devpost açıkça "About section" diyor).
- [ ] Devpost'ta proje sayfası aç, track seç (önerim: **Everyday Agents**).
- [ ] Demo videosu ≤ 5 dk. Kayıt: QuickTime (Cmd+Shift+5) ya da Loom. Yapı:
  1. Problem (60 sn): günde 60 bildirim, hepsi eşit görünüyor.
  2. Hedef kitle + neden önemli (45 sn).
  3. Canlı demo (2.5 dk): mail gelir → triyaj → Telegram'da 1 bildirim; "bugün kim yazdı?" sohbeti; taslak → düzelt → onayla → eski butona bas → reddedilir; Calendar'ı tek satırla eklediğimiz an.
  4. Mimari (30 sn): diyagram, AgentCore, "ajan DB'ye dokunmaz".
  Senaryoyu ben yazacağım; sen sadece okuyup çekersin.
- [ ] Mimari diyagramı: ben üretirim (`docs/architecture.png`), sen kontrol edersin.
- [ ] Bonus: builder.aws.com'da kısa "build journey" yazısı; taslağı README'den türetirim.

---

## E. Yardım edebileceğin veri işi (kod yazarken sıkılırsan)

- [ ] `tests/fixtures/labeled_triage.jsonl`: Faz 1'in eval'i için 30–60 sentetik mail + elle `urgency` etiketi (1–5). Ben 30 tanesini üretip etiketleyeceğim; sen etiketleri gözden geçir, katılmadıklarını düzelt. Gerçek kişisel veri **koyma**, repo public.
- [ ] Kendi kısa profilin (`USER_PROFILE` env'i, triyaj promptuna girer): "Kimsin, ne iş yaparsın, hangi konular acil sayılır" — 3-4 cümle. `.env`'de kalır, repo'ya sentetik bir örnek koyarız.

---

## Sırlar özeti (.env)

| Değişken | Kaynak | Hangi faz |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | BotFather + getUpdates | 0 |
| `COMPOSIO_API_KEY`, `COMPOSIO_WEBHOOK_SECRET`, `COMPOSIO_USER_ID` | Composio dashboard | 0 |
| `AWS_REGION`, `AWS_PROFILE` veya `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` | AWS | 1 |
| `TRIAGE_MODEL_ID`, `CHAT_MODEL_ID` | Bedrock model catalog | 1 |
| `AGENTCORE_RUNTIME_ARN` | `agentcore launch` çıktısı | 1 (opsiyonel) |
| `ANTHROPIC_API_KEY` | console.anthropic.com | 1 yedek |
| `DATABASE_URL` | compose (lokal) / Neon (deploy) | 0 / 3 |
