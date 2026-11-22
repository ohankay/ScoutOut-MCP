# D3 raporu: ops.CrmLog.Message serbest metin müşteri no kuralı

Ortam: sunucu 8704 (kurum LLM relay). Veritabanı: ScoutOutSynthSmall. Tarih: 2026-11-22.

## Adım 1: Bağlam kelimeli aday kural (GEÇTİ)

Araç: `mine_rules` (entries: ops.CrmLog.Message, type CUSTOMER_NO, hint: serbest metin, MN + 8 hane, bağlam kelimeleri).

- Üretilen aday: id 3, CUSTOMER_NO, kaynak llm-derived, durum CANDIDATE.
- Desen: `(?<![A-Za-z0-9])MN\d{8}(?![A-Za-z0-9])`
- Bağlam kelimeleri: müşteri, musteri. Kolon adı ipuçları: cust_no, musteri_no.
- Ölçümler: kesinlik 1,0 (hedef >= 0,9 karşılandı), özgüllük 1,0, duyarlılık 0,321. Pozitif 1000, negatif 2000 örnek.
- Mevcut kuraldan ayrı bir kayıt (id 3). Kural kendi bağlam kelimesiyle eşleşiyor.
- LLM'e 230 değer gönderildi; bu değerler ajana ve yanıtlara dönmedi.
- Ham değer yok. Yanıtta yalnızca desen, sayılar ve kelimeler var.

Not: Aday kural, etkinleştirilene kadar taramada kullanılmaz.

## Adım 2: Etkinleştirme ve yeniden tarama (GEÇTİ, bir not var)

- `decide_rule` (rule_id 3, activate): durum ACTIVE.
- `scan` (ScoutOutSynthSmall, ops.CrmLog, force=true): iş bae58531, 1 tablo, 4 kolon, süre yaklaşık 1 sn. Hatalı yok.
- `scan_status`: done.

Sonuçlar (`get_findings`, ops.CrmLog):

| Kolon | Bölge | Tür | Oran | Güven aralığı | Not |
|---|---|---|---|---|---|
| Message | SPARSE | CUSTOMER_NO | 0,3074 | 0,2584 - 0,3613 | Beklenen sonuç: seyrek müşteri no |
| Ref | SENSITIVE | CUSTOMER_NO | 1,0 | 0,9876 - 1,0 | Beklenen sonuç: hassas kalır. Çelişki bayrağı var (kolon adı hassas çağrıştırmıyor, içerikte bulundu) |
| Channel | CLEAN | - | 0 | - | |
| LogId | CLEAN | - | 0 | - | |

Message için `explain_finding`: 123 satır eşleşti (örneklem 400). Eşleşme iki kuralla da yakalanıyor: Onaylı kural #1 ve yeni kural #3, ikisi de 123 satır. Yani bağlam kelimeli kural bu veride ek kapsam sağlamadı; aynı satırları buluyor. Şans düzeltmesi 0. Değer içermiyor.

Notlar:
- Ref için çelişki bayrağı görüntüleniyor; yanlış alarm değil, gözden geçirme isteği. Kararı kullanıcı verir.
- Message'da çoğu satır metin içinde müşteri no barındırmıyor (seyrek). Bağlam kelimesi kuralın kesinliğini artırıyor ama bu tabloda kapsamı değiştirmedi.

## Adım 3: ops.LegacyImport DekontNo (GEÇTİ, beklenen mesaj değil)

Beklenen "henüz taranmadı" mesajı çıkmadı. LegacyImport bu sunucuda bugün 17:29:42'de taranmış. Sonuçlar mevcut:

- `get_findings` (tablo filtresi ops.LegacyImport): Aciklama SENSITIVE TCKN; YabanciKimlik SENSITIVE YKN; TCKN EMPTY; Kod, Tutar, DekontNo CLEAN.
- DekontNo: CLEAN, oran 0,0, 400 örnek.
- `explain_finding` (DekontNo): TCKN kontrol hanesi kuralı 5 satırda tuttu (ham oran 0,0125), şans düzeltmesi 3,9 satır düşürüldü, düzeltilmiş oran 0,0027, üst güven sınırı 0,0229 (temiz eşiğinin altı). YKN 0 satır. Sonuç: tesadüfi eşleşme, temiz.

Sonuç: DekontNo yanlış alarm vermiyor. Kolon 11 haneli ve sayı biçiminde ama TCKN tipi yalnızca tesadüfi kontrol-hanesi eşleşmeleri veriyor, bunlar düşülüyor.

## Özet

| Adım | Sonuç |
|---|---|
| 1. Bağlam kelimeli aday kural | GEÇTİ (kesinlik 1,0 >= 0,9) |
| 2. Etkinleştir ve yeniden tara | GEÇTİ. Message SPARSE, Ref SENSITIVE kaldı |
| 3. LegacyImport DekontNo | GEÇTİ. Önceki tarama sonucu var, CLEAN |

Gerçek değer: Hiçbir araç yanıtında gerçek değer görünmedi.
Süreler: tarama yaklaşık 1 sn; mine_rules bu çağrıda ölçülmedi, ama Bash zaman aşımı içinde tamamlandı.
Belirsizlik: Adım 3'te beklenen "taranmadı" yanıtı alınmadı, çünkü tablo daha önce taranmıştı.
