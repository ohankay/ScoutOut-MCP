# Grup B (Maskeleme denetimi) simülasyon raporu

## Özet

| No | Sonuç | Kullanılan araçlar (sırayla) |
|---|---|---|
| B1 | GEÇTİ | masking_sync |
| B2 | GEÇTİ | audit_masking, audit_masking(scan_missing=true), scan_status, audit_masking(scan_missing=true) |
| B3 | GEÇTİ | (B2 sonucunun özeti, ek araç yok) |
| B4 | GEÇTİ | scan(databases=[ScoutOutSynthSmall]), scan_status, audit_masking(scan_missing=true) |
| B5 | GEÇTİ | explain_finding |
| B6 | GEÇTİ | env.py add-masking (operatör, bir kez), masking_sync, audit_masking(scan_missing=true) |
| B7 | GEÇTİ | review_finding(approve) |
| B8 | GEÇTİ | scan(databases=[KartKopya]), scan_status, audit_masking(databases=[KartKopya], scan_missing=true) |
| B9 | GEÇTİ | export_report(kind=masking_audit) |

Toplam: 9 GEÇTİ, 0 KISMEN, 0 KALDI.

Gerçek değer kontrolü: Bu grupta çalıştırılan tüm araç cevaplarında kolon değeri, örnek satır veya ham değer görülmedi. Cevaplarda yalnızca zone, tür, oran, güven aralığı, sayım ve maskeleme tanımı bilgileri vardı.

---

## B1: Maskeleme listesini güncelle

- **Kullanıcının isteği:** "Maskeleme listesini ana sistemden güncelle; kaç tanım var?"
- **Araçlar:** `masking_sync {}`
- **Kullanıcıya cevap:** Maskeleme listesi ana sistemden okundu. 11 tanım var (sürüm 1).
- **Sonuç:** GEÇTİ. Beklenen 11 tanım (v1) ile uyuşuyor.
- **Notlar:** İlk senkronizasyon olduğu için `added: 11` döndü. Araç cevabı yeterli.

## B2: Maskeleme tanımları gerçekçi mi (ilk denetim)

- **Kullanıcının isteği:** "ScoutOutSynthSmall'daki maskeleme tanımları gerçekçi mi? Gereksiz veya eksik maskeleme var mı?"
- **Araçlar:**
  1. `audit_masking {"databases": ["ScoutOutSynthSmall"]}`: taranmamış 5 tablo raporlandı (bank.Card, bank.Customer, bank.TxnLog, ops.CustomerNote, ops.LegacyImport). `complete: false`, `correct: 0`, `tables_not_scanned: 5`.
  2. Ajan taramayı önerdi. Kullanıcı "evet, tara" dedi (sim kuralına göre onay varsayıldı).
  3. `audit_masking {"databases": ["ScoutOutSynthSmall"], "scan_missing": true}`: 5 tablo için tarama başladı (job 951e5452).
  4. `scan_status {"job_id": "951e5452"}`: status done, 5/5 tablo, 0 hata.
  5. `audit_masking {"databases": ["ScoutOutSynthSmall"], "scan_missing": true}`: `complete: true`.
- **Kullanıcıya cevap:** Önce 5 tablonun taranmadığını, sonuçların eksik olduğunu, taramayı öneririm dedim. Onaydan sonra tarama bitti ve denetim tamamlandı (ayrıntı B3'te).
- **Sonuç:** GEÇTİ. Taranmamış tablolar raporlandı, tarama onaydan sonra `scan_missing=true` ile başlatıldı, bittikten sonra denetim tekrar çalıştı.
- **Notlar:**
  - İlk çağrıdaki özette `missing: 0`, `algorithm: 0`, `unnecessary: 0` görünüyor. Sayılar taranmamış tablolar için 0. Mesaj ("Listedeki bazı tablolar henüz taranmadı") bunu açıklıyor, ama kullanıcı sayılara bakıp "sorun yok" diye okuyabilir. Özette `not_scanned` sayısının öne çıkması iyi olur.
  - Araç `scan_missing` ile yalnızca listedeki tabloları tarıyor. Bu açıklamada yazıyor, ama ad ve sonuç "tüm eksikler" izlenimi veriyor (bkz. B4).

## B3: Sonucu özetle

- **Kullanıcının isteği:** "Sonucu özetle."
- **Araçlar:** Ek çağrı yok. B2'nin son `audit_masking` cevabı kullanıldı.
- **Kullanıcıya cevap (özet):**
  - Gereksiz maskeleme (3): bank.TxnLog.Description, bank.TxnLog.RefNo, ops.LegacyImport.TCKN (boş kolon).
  - Algoritma uyumsuzluğu (2): bank.Customer.TCKN için S_RND_PHONE uyumsuz (mismatch); ops.CustomerNote.LongNote için S_NULLIFICATION yerine kısmi maskeleme yeterli olabilir (partial).
  - Veritabanında olmayan (2): bank.Customer.AnneKizlikSoyadi (kolon yok), ops.Arsiv2019.Aciklama (tablo yok).
  - Eksik maskeleme (12): bank.Card.MaskedPan, bank.Customer.BirthDate/GsmNo/Email/Address, ops.CustomerNote.Note03/Note08/Note50/Note92/Note99, ops.LegacyImport.Aciklama/YabanciKimlik.
  - Doğru (5).
- **Sonuç:** GEÇTİ. Beklenen liste ile uyuşuyor: gereksiz 3, algoritma 2 (Customer.TCKN, CustomerNote.LongNote), veritabanında olmayan 2, eksik içinde Customer.Email var.
- **Notlar:** bank.Account.Iban ve bank.Account.TaxNo bu aşamada listede yok, çünkü bank.Account maskeleme listesindeki tablolardan biri değil ve henüz taranmadı. Beklenen listede bunlar var ama B4'te görünüyor. Beklenen metin "gibi" ifadesiyle yazılmış, bu yüzden B3 geçti sayıldı; yalnızca kapsam farkı.

## B4: Eksik maskeleme listesini tam ver

- **Kullanıcının isteği:** "Eksik maskeleme listesini tam ver; listede olmayan tabloların taranmamış hassas kolonları da dahil olsun."
- **Araçlar:**
  1. `scan {"databases": ["ScoutOutSynthSmall"]}`: 8 tablo, 53 kolon (job 4afb555b). Değişmemiş 5 tablo taşındı.
  2. `scan_status {"job_id": "4afb555b"}`: done, 8/8, carried_unchanged 5.
  3. `audit_masking {"databases": ["ScoutOutSynthSmall"], "scan_missing": true}`: `complete: true`.
- **Kullanıcıya cevap:** Tüm veritabanı tarandı, denetim tamamlandı. 14 eksik maskeleme var: Account.Iban, Account.TaxNo, Card.MaskedPan, Customer.Address/BirthDate/Email/GsmNo, CustomerNote.Note03/08/50/92/99, LegacyImport.Aciklama/YabanciKimlik.
- **Sonuç:** GEÇTİ. 14 eksik, beklenen listenin tamamı (Customer 4, Account 2, Card 1, CustomerNote 5, LegacyImport 2).
- **Notlar:**
  - Kullanıcı "listede olmayan tabloları dahil et" dediğinde ajan `scan` (veritabanı) ve sonra `audit_masking` çağırmak zorunda kaldı. Araç açıklamasında bu adım yazıyor ama ilk bakışta `scan_missing` tek başına yeterli sanılabilir.

## B5: TxnLog.RefNo neden maskelenmesin

- **Kullanıcının isteği:** "TxnLog.RefNo'yu gerçekten maskelememize gerek yok mu? Kanıtın ne?"
- **Araçlar:** `explain_finding {"database": "ScoutOutSynthSmall", "table": "bank.TxnLog", "column": "RefNo"}`
- **Kullanıcıya cevap:** RefNo temiz (CLEAN). Hiçbir hassas tür için varlık kanıtı yok. Kontrol hanesi kuralı 400 örnekte 2 eşleşme verdi, ama şans düzeltmesiyle düşürüldü ve düzeltilmiş oran 0 çıktı. Güven aralığı [0; 0,0114]. Kolon 11 haneli rakamlardan oluşuyor. Maskelemeyi kaldırmak için onay verebilirsiniz.
- **Sonuç:** GEÇTİ. Gerekçe (kural, kanıt, şans düzeltmesi, güven aralığı) beklenen ile uyuşuyor. Değer yok.
- **Notlar:**
  - Kanıt satırında `matched_rows: 2` ama `chance_matches_removed: 4.0` görünüyor. Düzeltme gözlenen eşleşmeden büyük olduğu için düzeltilmiş oran 0 oluyor. Matematiksel olarak açıklanabilir, ama kullanıcı için tutarsız görünüyor; açıklamada bu ayrım yazılmamış.

## B6: Ana sistemde Email tanımı eklendi, tekrar kontrol

- **Kullanıcının isteği:** Operatör ana sisteme Email tanımını ekler (`env.py add-masking ... Email S_RND_MAIL`). Kullanıcı: "Ana sistemde liste değişti, tekrar kontrol et."
- **Araçlar:**
  1. `env.py add-masking ScoutOutSynthSmall bank Customer Email S_RND_MAIL "E-posta"` (operatör adımı, bir kez): "ana sistemde eklendi: ScoutOutSynthSmall.bank.Customer.Email (S_RND_MAIL)".
  2. `masking_sync {}`: version 2, columns 12, added 1 (ScoutOutSynthSmall.bank.Customer.Email).
  3. `audit_masking {"databases": ["ScoutOutSynthSmall"], "scan_missing": true}`: missing 14 -> 13, correct 5 -> 6, complete true.
- **Kullanıcıya cevap:** Ana sistemde 1 tanım eklendi (Customer.Email, S_RND_MAIL). Liste v2 oldu. Email artık eksik maskeleme listesinde değil.
- **Sonuç:** GEÇTİ. Beklenen 1 eklenen tanım, Email eksik listesinden çıktı.
- **Notlar:** Araç sürüm numarasını ve eklenen kolonu net veriyor.

## B7: RefNo için gereksiz maskeleme onayı

- **Kullanıcının isteği:** "RefNo için 'gereksiz maskeleme' sonucunu onaylıyorum, kaydet."
- **Araçlar:** `review_finding {"database": "ScoutOutSynthSmall", "table": "bank.TxnLog", "column": "RefNo", "decision": "approve", "reason": "Gereksiz maskeleme onaylandi (kullanici karari)"}`
- **Kullanıcıya cevap:** Karar kaydedildi: bank.TxnLog.RefNo onaylandı (zone CLEAN).
- **Sonuç:** GEÇTİ. Sonuç `review: APPROVED`.
- **Notlar:** Açıklama "sadece kullanıcı açıkça karar verdiğinde" diyor ve kullanıcı açıkça onayladı, uygun. `reason` alanını ajan kendi yazdı; kullanıcının kendi sözleriyle doldurulması daha doğru olabilir.

## B8: KartKopya için maskeleme denetimi

- **Kullanıcının isteği:** "KartKopya için de maskeleme denetimi yap."
- **Araçlar:**
  1. `scan {"databases": ["KartKopya"]}`: 8 tablo, 53 kolon (job a4008b76).
  2. `scan_status {"job_id": "a4008b76"}`: done, 8/8.
  3. `audit_masking {"databases": ["KartKopya"], "scan_missing": true}`: `complete: true`.
- **Kullanıcıya cevap:** KartKopya.bank.Card.CardNo doğru maskelenmiş. Diğer 18 hassas kolon maskelenmemiş görünüyor: Account.Iban/TaxNo, Card.MaskedPan, Customer.TCKN/FirstName/LastName/BirthDate/GsmNo/Email/Address, CustomerNote.Note03/08/50/92/99/LongNote, LegacyImport.Aciklama/YabanciKimlik.
- **Sonuç:** GEÇTİ. CardNo doğru (correct 1), diğer hassas kolonlar eksik.
- **Notlar:**
  - Eksik listesinde açıklama yok: ScoutOutSynthSmall için tanımlanmış FirstName/LastName gibi kolonların KartKopya'ya uygulanmadığı çıktıda yazmıyor. Kullanıcı "KartKopya'da da tanım mı yok?" diye sorabilir. Notta bir açıklama olsa iyi olur.

## B9: Denetimi Excel raporu olarak ver

- **Kullanıcının isteği:** "Denetimi Excel raporu olarak ver."
- **Araçlar:** `export_report {"kind": "masking_audit"}`
- **Kullanıcıya cevap:** Rapor oluşturuldu: D:\works\ScoutOut-MCP\sim_out\B\data\reports\scoutout_masking_audit_20261122-171126.xlsx (36 satır).
- **Sonuç:** GEÇTİ. Dosya yolu döndü, içerik dönmedi.
- **Notlar:** Rapor tüm veritabanları için üretildi (database parametresi boş). Ajan kapsamı kullanıcıya sormadı; sim için kabul edilebilir.

---

## Genel gözlemler

1. **Değer sızıntısı yok:** Bu grupta incelenen tüm cevaplarda gerçek değer görülmedi.
2. **Taranmamış tablo sayaçları:** Tarama yapılmadan çalıştırılan denetimde `missing: 0`, `algorithm: 0` görünüyor. Mesaj uyarıyor, ama sayılar "sorun yok" gibi okunabilir.
3. **scan_missing kapsamı:** Yalnızca listedeki tabloları tarıyor. Listede olmayan tablolar için veritabanı taraması gerekiyor (B4 ve B8). Açıklama bunu söylüyor, ajan iki adımı da yaptı.
4. **explain_finding tutarsızlığı (B5):** `matched_rows` ile `chance_matches_removed` arasında ilk bakışta açıklanmayan bir fark var.
5. **KartKopya gerekçesi (B8):** Eksik listesinde "ScoutOutSynthSmall tanımları buraya uygulanmaz" bilgisi yok.
6. **Araç açıklamaları:** Genel olarak yeterli ve Türkçe. Hata mesajı görülmedi.
