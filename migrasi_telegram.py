"""
migrasi_telegram.py
Migrasi anggota Telegram dari grup sumber ke grup tujuan.
Mode LIVE — langsung eksekusi (tanpa dry-run).
Gunakan dengan bijak. Patuhi ToS Telegram.
"""

import os
import sys
import time
import asyncio
import getpass
from datetime import datetime

from telethon import TelegramClient, functions, types
from telethon.errors import (
    FloodWaitError,
    UserPrivacyRestrictedError,
    UserNotMutualContactError,
    UserChannelsTooMuchError,
    UserAlreadyParticipantError,
    ChatAdminRequiredError,
    UserNotParticipantError,
    PeerFloodError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    ChannelPrivateError,
    InviteHashExpiredError,
    InviteHashInvalidError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SESSION_DIR = "session"
LOG_FILE = "hasil_migrasi.txt"

# Jeda antar-invite (detik). Naikkan jika sering FloodWait.
DELAY_BETWEEN_INVITES = 8


# ============================================================
# UTILITAS
# ============================================================
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def header(text):
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)


def ambil_input_konfigurasi():
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    phone = os.getenv("TG_PHONE")

    if not api_id:
        api_id = input("Masukkan API ID: ").strip()
    else:
        print(f"[.env] API ID: {api_id[:4]}****")

    if not api_hash:
        api_hash = input("Masukkan API Hash: ").strip()
    else:
        print(f"[.env] API Hash: {api_hash[:4]}****")

    if not phone:
        phone = input("Masukkan nomor HP (contoh +62812...): ").strip()
    else:
        print(f"[.env] Nomor HP: {phone[:5]}****")

    try:
        api_id_int = int(api_id)
    except ValueError:
        print("[!] API ID harus berupa angka.")
        sys.exit(1)

    return api_id_int, api_hash, phone


def resolve_target(client, target_str):
    """
    Resolve username/link/ID menjadi entity Telegram.
    Mendukung: @username, https://t.me/xxx, https://t.me/+HASH, -100xxx
    """
    t = target_str.strip()

    # Invite link privat
    if "t.me/+" in t or "joinchat/" in t:
        hash_part = t.split("+")[-1] if "t.me/+" in t else t.split("joinchat/")[-1]
        try:
            invite_info = client(CheckChatInviteRequest(hash_part))
            if isinstance(invite_info, types.ChatInviteAlready):
                return invite_info.chat
            else:
                raise ValueError(
                    "Anda belum bergabung ke grup tujuan ini. "
                    "Join dulu via link tersebut, lalu jalankan ulang."
                )
        except InviteHashExpiredError:
            raise ValueError("Link undangan kadaluarsa.")
        except InviteHashInvalidError:
            raise ValueError("Link undangan tidak valid.")

    if t.startswith("https://t.me/"):
        t = t.replace("https://t.me/", "")
    if t.startswith("t.me/"):
        t = t.replace("t.me/", "")

    try:
        return client.get_entity(t)
    except UsernameInvalidError:
        raise ValueError(f"Username tidak valid: {t}")
    except UsernameNotOccupiedError:
        raise ValueError(f"Username tidak ditemukan: {t}")
    except ChannelPrivateError:
        raise ValueError(f"Grup privat / tidak ada akses: {t}")
    except Exception as e:
        raise ValueError(f"Gagal resolve '{t}': {e}")


async def pastikan_join(client, entity):
    """Pastikan akun sudah di grup tujuan."""
    try:
        await client.get_permissions(entity, "me")
        return True, "sudah bergabung"
    except UserNotParticipantError:
        pass
    except Exception:
        pass

    try:
        await client(functions.channels.JoinChannelRequest(entity))
        return True, "berhasil join"
    except Exception as e:
        return False, f"tidak bisa join: {e}"


async def ambil_anggota(client, entity):
    """Ambil anggota yang bisa diproses. Return list (id, username, nama)."""
    anggota = []
    try:
        async for user in client.iter_participants(entity):
            if user.bot or user.deleted:
                continue
            username = f"@{user.username}" if user.username else None
            nama = (user.first_name or "") + (
                " " + user.last_name if user.last_name else ""
            )
            anggota.append((user.id, username, nama.strip() or "(tanpa nama)"))
    except ChatAdminRequiredError:
        raise ValueError("Anda bukan admin grup sumber — tidak bisa baca anggota.")
    except Exception as e:
        raise ValueError(f"Gagal ambil anggota: {e}")
    return anggota


def simpan_log(lines, path=LOG_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n[✓] Log disimpan: {path}")
    except Exception as e:
        print(f"[!] Gagal simpan log: {e}")


def format_durasi(detik):
    m, s = divmod(int(detik), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}j {m}m {s}d"
    if m:
        return f"{m}m {s}d"
    return f"{s}d"


# ============================================================
# PROSES UTAMA (LIVE)
# ============================================================
async def proses_invite(client, grup_tujuan, anggota):
    stats = {
        "berhasil": 0,
        "sudah_ada": 0,
        "gagal": 0,
        "detail_berhasil": [],
        "detail_sudah_ada": [],
        "detail_gagal": [],
    }

    total = len(anggota)
    for idx, (uid, uname, nama) in enumerate(anggota, start=1):
        label = uname or f"id:{uid}"
        print(f"[{idx}/{total}] Memproses {label} ...", end=" ")

        try:
            user_entity = await client.get_entity(uid)
        except Exception as e:
            print(f"GAGAL (resolve: {e})")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - resolve: {e}")
            continue

        try:
            await client(InviteToChannelRequest(channel=grup_tujuan, users=[user_entity]))
            print("BERHASIL")
            stats["berhasil"] += 1
            stats["detail_berhasil"].append(f"{label} ({nama})")

        except UserAlreadyParticipantError:
            print("SUDAH ADA")
            stats["sudah_ada"] += 1
            stats["detail_sudah_ada"].append(f"{label} ({nama})")

        except FloodWaitError as e:
            print(f"FloodWait {e.seconds}s...")
            await asyncio.sleep(e.seconds + 2)
            try:
                await client(InviteToChannelRequest(channel=grup_tujuan, users=[user_entity]))
                print("  -> BERHASIL setelah tunggu")
                stats["berhasil"] += 1
                stats["detail_berhasil"].append(f"{label} ({nama}) [retry]")
            except Exception as e2:
                print(f"  -> GAGAL: {e2}")
                stats["gagal"] += 1
                stats["detail_gagal"].append(f"{label} ({nama}) - {e2}")

        except UserPrivacyRestrictedError:
            print("GAGAL (privasi user)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - privasi user")

        except UserNotMutualContactError:
            print("GAGAL (bukan kontak mutual)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - bukan kontak mutual")

        except UserChannelsTooMuchError:
            print("GAGAL (user di terlalu banyak channel)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - channel terlalu banyak")

        except PeerFloodError:
            print("GAGAL (PeerFlood)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - PeerFlood")
            print("\n[!] PeerFlood terdeteksi. Hentikan dan tunggu 24 jam+.")
            break

        except ChatAdminRequiredError:
            print("GAGAL (bukan admin grup tujuan)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - butuh admin")
            break

        except Exception as e:
            print(f"GAGAL ({e})")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - {e}")

        await asyncio.sleep(DELAY_BETWEEN_INVITES)

    return stats


# ============================================================
# MAIN
# ============================================================
async def main_async():
    clear_screen()
    header("MIGRASI ANGGOTA TELEGRAM — MODE LIVE")

    print("""
[!] MODE LIVE: anggota akan benar-benar ditambahkan ke grup tujuan.
[!] Gunakan hanya untuk grup yang Anda kelola sendiri.
[!] Telegram dapat membatasi akun jika dianggap spam.
""")

    api_id, api_hash, phone = ambil_input_konfigurasi()

    os.makedirs(SESSION_DIR, exist_ok=True)
    session_path = os.path.join(SESSION_DIR, "migrasi")

    client = TelegramClient(session_path, api_id, api_hash)
    await client.start(
        phone=phone,
        password=lambda: getpass.getpass("Password 2FA (kosongkan jika tidak ada): "),
    )

    me = await client.get_me()
    print(f"\n[✓] Login sebagai: {me.first_name} (@{me.username or 'tanpa username'})")

    sumber_str = input("\nUsername/link/ID grup SUMBER: ").strip()
    tujuan_str = input("Username/link/ID grup TUJUAN: ").strip()

    print("\n[·] Resolve grup sumber...")
    try:
        grup_sumber = await resolve_target(client, sumber_str)
    except ValueError as e:
        print(f"[!] {e}")
        await client.disconnect()
        return

    print("[·] Resolve grup tujuan...")
    try:
        grup_tujuan = await resolve_target(client, tujuan_str)
    except ValueError as e:
        print(f"[!] {e}")
        await client.disconnect()
        return

    print(f"[✓] Sumber: {getattr(grup_sumber, 'title', grup_sumber)}")
    print(f"[✓] Tujuan: {getattr(grup_tujuan, 'title', grup_tujuan)}")

    ok, msg = await pastikan_join(client, grup_tujuan)
    print(f"[i] Keanggotaan grup tujuan: {msg}")
    if not ok:
        print("[!] Tidak bisa lanjut tanpa bergabung ke grup tujuan.")
        await client.disconnect()
        return

    print("\n[·] Mengambil daftar anggota grup sumber...")
    try:
        anggota = await ambil_anggota(client, grup_sumber)
    except ValueError as e:
        print(f"[!] {e}")
        await client.disconnect()
        return

    if not anggota:
        print("[!] Tidak ada anggota yang bisa diproses.")
        await client.disconnect()
        return

    print(f"\n[✓] Ditemukan {len(anggota)} anggota yang dapat diproses.")
    print("    (bot & akun terhapus otomatis difilter)")

    konfirmasi = input("\nMulai proses pemindahan anggota? [y/n]: ").strip().lower()
    if konfirmasi != "y":
        print("[i] Dibatalkan.")
        await client.disconnect()
        return

    header("MEMULAI PROSES LIVE")
    mulai = time.time()
    stats = await proses_invite(client, grup_tujuan, anggota)
    durasi = time.time() - mulai

    # Log
    log_lines = [
        f"# Hasil Migrasi Telegram - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Sumber : {getattr(grup_sumber, 'title', sumber_str)}",
        f"# Tujuan : {getattr(grup_tujuan, 'title', tujuan_str)}",
        f"# Mode   : LIVE",
        "",
        f"## BERHASIL ({len(stats['detail_berhasil'])})",
        *(stats["detail_berhasil"] or ["-"]),
        "",
        f"## SUDAH ADA ({len(stats['detail_sudah_ada'])})",
        *(stats["detail_sudah_ada"] or ["-"]),
        "",
        f"## GAGAL ({len(stats['detail_gagal'])})",
        *(stats["detail_gagal"] or ["-"]),
    ]

    simpan = input("\nSimpan log ke hasil_migrasi.txt? [y/n]: ").strip().lower()
    if simpan == "y":
        simpan_log(log_lines)

    header("RINGKASAN")
    print(f"Total anggota ditemukan : {len(anggota)}")
    print(f"Berhasil                : {stats['berhasil']}")
    print(f"Sudah ada               : {stats['sudah_ada']}")
    print(f"Gagal                   : {stats['gagal']}")
    print(f"Total waktu proses      : {format_durasi(durasi)}")

    await client.disconnect()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[i] Dihentikan pengguna.")
    except Exception as e:
        print(f"\n[!] Error fatal: {e}")


if __name__ == "__main__":
    main()
