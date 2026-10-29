# Legacy NetLink stabilization runbook

Минимальный hardening текущего production NetLink 1.x (снижение риска повторного
падения), пока строится NetLink 2.0. Ничего не переписывает, только стабильность.

## Что решает

| Проблема (факт аудита 29.10.2026) | Решение |
|---|---|
| Xray работает с `loglevel=debug`; error-лог растёт ~сотни МБ/сутки (452M + 135M на момент аудита); на этом сервере уже был OOM-kill Xray 26.06.2026 | `scripts/set_xray_loglevel.py`: правит source of truth (settings.xrayTemplateConfig в БД 3x-ui) → `warning`; регенерация конфига панелью больше не вернёт debug |
| Ротация xray-логов была `size 100M, rotate 3` без daily | `deploy/xray-logs`: daily + size 100M + rotate 7 + compress + delaycompress + copytruncate (copytruncate сохранён — Xray держит логи открытыми, сигнал не нужен) |
| RAM 1.9 GiB, swap отсутствует (прецедент OOM) | `scripts/setup_swap.sh`: 2 GiB /swapfile, 0600, persistent fstab, vm.swappiness=10, идемпотентно |
| journald ~1.7 GiB без лимитов | `scripts/install_journald_limits.sh`: drop-in SystemMaxUse=256M / RuntimeMaxUse=64M / MaxRetentionSec=14day + vacuum |
| netlink.db без регулярного бэкапа | `scripts/netlink_db_backup.py` + `deploy/netlink-db-backup.{service,timer}`: daily 03:30 UTC, SQLite Online Backup API (консистентно на живой базе), PRAGMA integrity_check копии, retention 14, /var/backups/netlink 0700 |

## Source of truth для loglevel

Генерируемый `/usr/local/x-ui/bin/config.json` панель 3x-ui перезаписывает из
своей БД (`/etc/x-ui/x-ui.db`, ключ `xrayTemplateConfig`). Ручная правка
генерированного файла бесполезна и в этой схеме НЕ используется. Скрипт меняет
именно шаблон в БД (единственное поле `log.loglevel`), делает timestamped
бэкап старого значения, читает обратно для проверки. Для применения нужен
restart x-ui (панель перегенерирует конфиг) — выполняется только ручным
workflow, не скриптом.

Access-log НЕ отключается: его парсит netlink-ip-monitor (детект шаринга).

## Как применяется (только вручную)

GitHub Actions → **Stabilize legacy NetLink (manual)** → Run workflow →
в поле confirm ввести `stabilize`.

Шаги workflow (полностью идемпотентны, повторный запуск безопасен):
1. Локальный preflight (наличие всех файлов).
2. GATE identity: hostname `ubuntu-Amsterdam-2gb` + наличие IP на интерфейсах +
   совпадение egress IP; при несовпадении — ABORT до любых изменений.
3. Синхронизация кода официальным каналом деплоя (git fetch + reset origin/main
   в /opt/netlink-bot; никаких scp/rsync).
4. Timestamped snapshot всего, что будет затронуто: консистентная копия
   x-ui.db (sqlite .backup), /etc/logrotate.d/xray, /etc/fstab, journald
   drop-ins, снимки free/df/swapon → `/root/netlink-stabilize-backups/<ts>/`.
5. Установка logrotate-политики и backup-юнитов; первый бэкап немедленно.
6. loglevel → warning в БД + `systemctl restart x-ui` (краткий реконнект
   клиентов, секунды) + проверка: x-ui active, config.json = warning, :443 слушает.
7. Swap (2G) + swappiness 10.
8. Journald drop-in + restart journald (штатно-безопасная операция) + vacuum.
9. After-отчёт: free/swapon/df/journal usage/logrotate validation/статусы
   сервисов и таймеров/healthz/:443. Reboot НЕ выполняется.

Отдельные шаги НЕ выполняются автоматически по ping/мониторам — any self-healing
restart только после отдельного проектирования health-check (вне скоупа).

## Rollback

- loglevel: восстановить значение из
  `/root/netlink-stabilize-backups/<ts>/xrayTemplateConfig.*.json`
  (или тем же скриптом с `--loglevel debug --apply`), затем `systemctl restart x-ui`.
- logrotate: вернуть `/root/netlink-stabilize-backups/<ts>/logrotate.xray`.
- swap: `swapoff /swapfile`, удалить строку из /etc/fstab, удалить файл.
- journald: удалить drop-in `/etc/systemd/journald.conf.d/90-netlink-hardening.conf`,
  `systemctl restart systemd-journald`.
- backup units: `systemctl disable --now netlink-db-backup.timer` и удалить юниты.

## Ограничение

Бэкап netlink.db — локальный (/var/backups на том же VPS). Это защита от
случайной порчи/удаления, НЕ disaster recovery при потере VPS. Off-site backup —
отдельная задача NetLink 2.0.

## Тесты

```bash
bash tests/run_tests.sh
```

26 проверок: идемпотентность скриптов, консистентность+integrity бэкапа,
retention, отсутствие секретов в stdout, валидность logrotate-конфига,
статическая валидация workflows (только manual dispatch, gates, отсутствие
reboot-команд).
