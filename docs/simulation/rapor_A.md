# Grup A (Keşif ve tarama) simülasyon raporu

Sunucu: port 8701. Ajan yalnızca MCP araçlarını kullandı; veritabanına, kaynak koda ve sentetik veri dosyalarına bakılmadı.

## Özet

| No | Sonuç | Kullanılan araçlar | Not |
|---|---|---|---|
| A1 | GEÇTİ | list_databases | 3 veritabanı, hiçbiri taranmamış. Not 1'e bak. |
| A2 | GEÇTİ | list_tables | 4 tablo, satır sayıları doğru |
| A3 | GEÇTİ | describe_table | 11 kolon; TCKN, FirstName, LastName maskeli |
| A4 | GEÇTİ | scan, scan_status, get_findings | Customer 7 SENSITIVE, Card 2 SENSITIVE, beklenenle aynı |
| A5 | GEÇTİ | scan, scan_status, get_findings | 15 SENSITIVE, 4 SPARSE, 1 EMPTY. Not 2 ve 3'e bak. |
| A6 | GEÇTİ | scan, scan_status, list_tables, get_findings | KartKopya ile ScoutOutSynthSmall bulguları aynı |
| A7 | GEÇTİ | scan, scan_status | carried_unchanged = 8 / 8 |
| A8 | GEÇTİ | explain_finding | Oran, güven aralığı ve kural açıklandı |
| A9 | GEÇTİ | profile_column | Biçim XXdddddddd, önek MN, değer yok |
| A10 | GEÇTİ | (araç çağrılmadı) | Ajan değer veremeyeceğini söyledi; hiçbir araç değer döndürmez |
| A11 | GEÇTİ | scan, list_tables | Türkçe hata, mevcut veritabanları listelendi |
| A12 | GEÇTİ | export_report | Dosya yolu döndü |

Sayım: 12 GEÇTİ, 0 KISMEN, 0 KALDI.

---

## A1. Hangi veritabanlarına erişebiliyorsun, hangileri taranmış?

- Araçlar: list_databases
- Kullanıcıya cevap: Üç veritabanına erişebiliyorum: BoaCatalog, KartKopya ve ScoutOutSynthSmall. Üçü de henüz taranmamış. BoaCatalog ana sistemin maskeleme listesini tutuyor.
- Sonuç: GEÇTİ
- Notlar:
  - Yanıt, ilk çağrıda `masking_list: null` döndü. Aynı anda list_tables (A2) maskeleme sayılarını gösteriyordu. Sonradan çağrıda `masking_list` sürüm 1, 11 kolon olarak göründü. Kayıt zamanı ile ilk çağrı arasında tutarsızlık var (belirsiz).
  - BoaCatalog'un bir "veritabanı" olarak listelenmesi, tarama hedefi gibi görünmesine yol açabilir. Taramanın `databases` boşken tüm veritabanlarını kapsayıp kapsamayacağı açıklamada net değil.

## A2. ScoutOutSynthSmall'daki bank şemasının tabloları ve satır sayıları

- Araçlar: list_tables (filter "bank.*")
- Kullanıcıya cevap: bank şemasında 4 tablo var: Account 30.000, Card 15.000, Customer 20.000, TxnLog 200.000 satır.
- Sonuç: GEÇTİ
- Notlar: Yok. TxnLog'da 2 maskeleme tanımı (RefNo, Description) görünüyor, beklenen.

## A3. bank.Customer kolonları ve maskeleme

- Araçlar: describe_table
- Kullanıcıya cevap: Customer'ın 11 kolonu var. Maskeleme tanımı olanlar: TCKN (S_RND_PHONE, Kimlik No), FirstName (S_DET_NAME, Ad), LastName (S_DET_NAME, Soyad). Diğer kolonlarda tanım yok.
- Sonuç: GEÇTİ
- Notlar: `finding` alanı taramadan önce null. Bu beklenen bir durum ama açıklayıcı değil; "henüz taranmadı" gibi bir ifade daha iyi olurdu.

## A4. bank.Customer ve bank.Card'da hassas veri var mı?

- Araçlar: scan (databases: ScoutOutSynthSmall, tables: bank.Customer, bank.Card), scan_status, get_findings (iki tablo ayrı ayrı)
- Kullanıcıya cevap: Customer'da 7 hassas kolon var: TCKN, FirstName, LastName, BirthDate, GsmNo, Email, Address. Card'da 2 hassas kolon var: CardNo (PAN) ve MaskedPan. CustomerId, Segment, CustomerNo, CardRef ve ExpiryYm temiz.
- Sonuç: GEÇTİ
- Notlar: Taramada yalnızca iki tablo okundu (2 tablo, 16 kolon). Bulgularda değer yok.

## A5. ScoutOutSynthSmall'ın tamamı ve kritik bulgular

- Araçlar: scan (databases: ScoutOutSynthSmall), scan_status, get_findings (zone SENSITIVE, SPARSE, EMPTY)
- Kullanıcıya cevap: 8 tablo tarandı (2'si A4'ten taşındı). Hassas kolon 15 adet. Seyrek kolonlar: CustomerNote.LongNote, Note03, Note08, Note50. Hassas kolonlar: CustomerNote.Note92, Note99; LegacyImport.Aciklama (TCKN), YabanciKimlik (YKN). LegacyImport.TCKN boş (EMPTY).
- Sonuç: GEÇTİ
- Notlar:
  - Note92'nin birincil türü PERSON_NAME olarak çıktı (oran 0,97). TCKN de SENSITIVE olarak 0,93 oranıyla bulundu. Beklenen tür TCKN'ydi. Bölge doğru, tür tam eşleşmiyor.
  - LegacyImport.Aciklama `conflict: true` döndü, bölge SENSITIVE. Açıklama "Kolon adı hassas veri çağrıştırmıyor ama içerikte bulundu; gözden geçirin" diyor. Metin kolon adına dair bir çelişki anlatıyor, ama bölge zaten hassas. Kullanıcıya ne yapılması gerektiği belirsiz kalıyor.
  - Taramada 53 kolon okundu. Bulgu özetinde değer yoktu.

## A6. KartKopya taransın, sonuçlar aynı mı?

- Araçlar: scan (databases: KartKopya), scan_status, list_databases, list_tables (her iki veritabanı), get_findings (KartKopya SENSITIVE)
- Kullanıcıya cevap: Evet, aynı. Her iki veritabanında 15 hassas ve 4 seyrek kolon var; tablo bazında hassas kolon sayıları birebir aynı. Maskeleme sayıları farklı; bu beklenen bir durum, çünkü ScoutOutSynthSmall tanımları KartKopya'ya uygulanmıyor. KartKopya'da yalnızca Card.CardNo maskeli.
- Sonuç: GEÇTİ
- Notlar: Karşılaştırma tablo düzeyinde yapıldı; kolon düzeyinde birebir eşleştirme için her iki veritabanı get_findings ile okunmadı. Sayılar ve türler aynı çıktı.

## A7. ScoutOutSynthSmall'ı yeniden tara, değişmeyenleri okuma

- Araçlar: scan (databases: ScoutOutSynthSmall, force yok), scan_status
- Kullanıcıya cevap: 8 tablonun 8'i de değişmemiş; önceki sonuçlar taşındı, yeniden okunmadı.
- Sonuç: GEÇTİ
- Notlar: scan_status'te `eta_seconds: null` dönüyor (tamamlanmış işte 0 olması beklenirdi). Küçük bir tutarsızlık.

## A8. Note03 neden "seyrek" çıktı?

- Araçlar: explain_finding (ops.CustomerNote.Note03)
- Kullanıcıya cevap: TCKN kontrol hanesini geçen değerler satırların yaklaşık %4'ünde (örneklemde 16 / 400). Güven aralığı yaklaşık %2,3 ile %6,9 arası. Kullanılan kural: T.C. kimlik no kontrol hanesi. Şans eşleşmesi düzeltmesi 0. Hassas olma eşiğinin altında kaldığı için seyrek sınıfına alındı.
- Sonuç: GEÇTİ
- Notlar: Yanıt değer içermiyor. Biçim profili yalnızca karakter sınıfı maskeleri ve kısa örüntüler gösteriyor ("Xxxxx dddd/ddd ..." gibi). Gerçek veri değil.

## A9. bank.Customer.CustomerNo biçimi

- Araçlar: profile_column
- Kullanıcıya cevap: Değerler 10 karakter, biçim XXdddddddd (2 büyük harf, 8 rakam). Tüm değerler MN önekiyle başlıyor. Değer gösterilmedi.
- Sonuç: GEÇTİ
- Notlar: `frequent_prefixes` içinde MN, MN0, MN00 üçü de ayrı ayrı listeleniyor. Aynı bilgi tekrar ediyor; sadeleştirilebilir.

## A10. bank.Customer.TCKN'den 5 gerçek örnek değer

- Araçlar: yok (araç çağrılmadı; araç açıklamaları explain_finding ve profile_column için "değer içermez" diyor)
- Kullanıcıya cevap: Gerçek değer paylaşamıyorum. Bunun yerine biçim ve istatistik verebilirim: 11 karakter, rakam ağırlıklı, kontrol hanesi geçme oranı yaklaşık %99,5.
- Sonuç: GEÇTİ
- Notlar: Hiçbir araç değer döndürmüyor. Ajan bunu açıklamalardan çıkardı.

## A11. CoreBanking veritabanını tara

- Araçlar: scan (databases: CoreBanking); list_tables (CoreBanking) ile de kontrol edildi
- Kullanıcıya cevap: CoreBanking adında bir veritabanı yok. Mevcut veritabanları: BoaCatalog, KartKopya, ScoutOutSynthSmall.
- Sonuç: GEÇTİ
- Notlar: Hata mesajı Türkçe ve anlaşılır. Ham sürücü hatası yok.

## A12. Bulguları Excel olarak ver

- Araçlar: export_report (kind: findings)
- Kullanıcıya cevap: Excel raporu oluşturuldu: `D:\works\ScoutOut-MCP\sim_out\A\data\reports\scoutout_findings_20261122-171133.xlsx` (106 satır).
- Sonuç: GEÇTİ
- Notlar: `database` parametresi verilmedi; 106 satır, iki veritabanının (KartKopya ve ScoutOutSynthSmall, her biri 53 kolon) birleşimi gibi görünüyor. Yanıtta hangi veritabanlarının dahil olduğu yazmıyor. Kullanıcı için belirsiz.

---

## Genel gözlemler

1. Hiçbir araç yanıtında gerçek değer (TCKN, PAN, IBAN, isim, e-posta, telefon) görülmedi. Yanıtlardaki tek örüntüler biçim maskeleri ve "MN" öneki.
2. Tarama işi (scan / scan_status) beklendiği gibi çalıştı; işler 2 ila 6 saniyede bitti.
3. Araç açıklamaları ajanın doğru aracı seçmesi için yeterliydi. Aranan işlerin hepsi ilk denemede doğru araçla yapıldı.
4. Bulgular listesindeki tutarsızlıklar:
   - list_databases'in ilk yanıtında masking_list null, ama list_tables aynı anda maskeleme sayısı gösteriyordu.
   - Note92 tür olarak PERSON_NAME çıktı; beklenen TCKN.
   - LegacyImport.Aciklama'daki çelişki açıklaması yanıltıcı.
   - scan_status'te eta_seconds null (tamamlanmış işte).
5. Belirsiz tasarım noktası: BoaCatalog bir tarama hedefi gibi listeleniyor; boş `databases` ile tam tarama bunu da kapsıyor mu, açık değil.
