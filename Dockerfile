FROM python:3.12-slim

# Geliştirme araçları + SSH sunucusu
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openssh-server vim less procps htop git curl sudo \
 && rm -rf /var/lib/apt/lists/*

# SSH host anahtarlarını imaja bırak (konteyner yeniden oluşturulana kadar sabit)
RUN ssh-keygen -A

# dev kullanıcısı: parolasız sudo
RUN useradd -m -s /bin/bash dev \
 && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev \
 && chmod 0440 /etc/sudoers.d/dev

# Sadece anahtarla giriş: şifre ve root girişi kapalı
RUN sed -i \
      -e 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' \
      -e 's/^#\?PermitRootLogin.*/PermitRootLogin no/' \
      -e 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' \
      /etc/ssh/sshd_config \
 && mkdir -p /run/sshd

WORKDIR /workspace
EXPOSE 22

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
