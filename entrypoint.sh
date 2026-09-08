#!/bin/sh
# SSH sunucusunu dev kullanıcısı için hazırla ve başlat.
# authorized_keys dışarıdan /keys/authorized_keys olarak salt-okunur mount edilir;
# sshd StrictModes doğru sahiplik/izin istediği için kopya ile yerleştirilir.
set -e

mkdir -p /run/sshd
install -d -m 700 -o dev -g dev /home/dev/.ssh

if [ -f /keys/authorized_keys ]; then
    install -m 600 -o dev -g dev /keys/authorized_keys /home/dev/.ssh/authorized_keys
else
    echo "UYARI: /keys/authorized_keys bulunamadi — SSH ile giris olmayacak." >&2
fi

exec /usr/sbin/sshd -D -e
