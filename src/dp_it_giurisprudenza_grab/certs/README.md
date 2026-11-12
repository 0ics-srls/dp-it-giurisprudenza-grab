# Pinned intermediate certificate

`def.finanze.it` (MEF DocTrib) sends only its leaf certificate. The issuing intermediate is missing from the TLS handshake, so a client that has only root certificates (certifi) fails with `unable to verify the first certificate` (found with `openssl s_client -connect def.finanze.it:443 -servername def.finanze.it -showcerts`, verify code 21). Browsers hide this by fetching the intermediate from the AIA extension; this class does not fetch anything at run time and pins the file instead.

| Item | Value |
|---|---|
| File | `sectigo-public-server-authentication-ca-ov-r36.pem` |
| Source URL | http://crt.sectigo.com/SectigoPublicServerAuthenticationCAOVR36.crt (the `CA Issuers` URI of the DocTrib leaf certificate; DER, converted to PEM) |
| Subject | C=GB, O=Sectigo Limited, CN=Sectigo Public Server Authentication CA OV R36 |
| Issuer | C=GB, O=Sectigo Limited, CN=Sectigo Public Server Authentication Root R46 (a root in certifi) |
| Valid | 2021-03-22 to 2036-03-21 |
| Subject key identifier | E3:66:74:BB:70:68:8D:2C:5D:4E:0E:A6:4A:8F:9B:37:22:9C:82:92 (equals the authority key identifier of the leaf) |
| Basic constraints | CA:TRUE, pathlen:0 |
| SHA-256 | 65:42:D1:76:BE:D5:0F:19:3C:0C:E2:97:AE:44:EC:D8:A0:A8:6B:EC:2E:DE:68:27:69:34:40:59:B4:E7:85:30 |

An intermediate can only vouch for certificates it signed, and the chain still has to end at a certifi root, so trusting it widens nothing beyond Sectigo OV certificates. `http.ssl_context(verify_tls=True)` loads certifi plus this file. The test `tests/unit/test_tls_bundle.py` checks that the fingerprint above equals the file.

To renew: download the URL above, `openssl x509 -inform DER -in x.crt -out new.pem`, compare the subject key identifier with the leaf, update the fingerprint here, add a new tag.
