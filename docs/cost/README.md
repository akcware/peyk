# Maliyet modeli ve rapor

`report.pdf` — Peyk'in kullanıcı başına model maliyeti ve açık ağırlıklı model
alternatiflerinin değerlendirmesi.

## Neyin ölçüm, neyin varsayım olduğu

**Ölçüm (yüksek güven).** Token sayıları bu depodan çıkarılmıştır; kaynakları raporun
Ek B'sinde dosya adıyla listelidir. Karakter sayısının 4'e bölünmesiyle elde edildikleri
için ±%30 pay taşırlar. Gerçek rakamlar ancak her model çağrısının `usage` alanı
kaydedildiğinde bilinir — raporun Faz 0 önerisi budur.

**Varsayım (doğrulanmalı).** Fiyatların hiçbiri canlı kaynaktan teyit edilememiştir;
raporun hazırlandığı ortamın ağ politikası `aws.amazon.com/bedrock/pricing`,
`openrouter.ai`, `artificialanalysis.ai` ve `huggingface.co` dahil tüm fiyat
kaynaklarını 403 ile reddetmiştir. Anthropic satırları liste fiyatıdır; Bedrock ayrı
fiyatlandırılır. Açık model fiyatları tedarikçi iddiası olarak değil **fiyat bandı**
olarak modellenmiştir, tam da bu yüzden.

## Yeniden üretme

```bash
python3 model.py            # okunabilir tablolar (terminal)
python3 model.py --emit .   # t1..t7.tex — LaTeX tabloları
pdflatex report.tex         # iki kez (içindekiler için)
```

Değiştirilebilecek parametreler `model.py` içinde:

| Parametre | Ne | Varsayılan |
|---|---|---|
| `PRICES` | $/MTok giriş-çıkış, model başına | liste fiyatları + 4 bant |
| `PROFILES` | (mail/gün, sohbet turu/gün) | 20/3, 50/8, 120/20 |
| `SCEN` | kümülatif senaryolar | S0–S8 |
| `CACHE_MIN` | minimum önbelleklenebilir önek | Haiku 4.5: 4096 |
| `LTV`, `CHURN_GIVEN_MISS` | kalite riski varsayımları | $300, 0.5 |

Bedrock'un gerçek fiyatları teyit edildiğinde yalnızca `PRICES` güncellenir; rapordaki
her tablo yeniden üretilir.

## Notlar

- LaTeX'in `\input`'u bir `tabular` **içinde** çalışmaz (dosya kancaları `\par` enjekte
  eder). Bu yüzden `emit_tabular()` her parçayı eksiksiz bir `tabular` olarak yazar ve
  `report.tex` onu float içine alır.
- Derleme için gereken paketler: `texlive-latex-base`, `texlive-latex-recommended`,
  `texlive-latex-extra`, `texlive-fonts-recommended`, `texlive-lang-european` (Türkçe
  tireleme), `lmodern`.
