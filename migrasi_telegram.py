"""
migrasi_telegram.py
Script bantuan migrasi anggota Telegram dari satu grup ke grup lain.
Dibuat untuk Termux + Telethon. Gunakan dengan bijak.
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
    ImportChatInviteRequest,
)

# ---------- Opsi dotenv (opsional) ----------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SESSION_DIR = "session"
LOG_FILE = "hasil_migrasi.txt"

# Jeda aman antar request (detik). Perbesar jika akun sering kena FloodWait.
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
    """
    Ambil API ID, API Hash, dan nomor HP.
    Prioritas: file .env -> input interaktif.
    """
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
    Mendukung:
      - @username
      - https://t.me/username
      - https://t.me/+HASH (invite link privat)
      - -100xxxxxxxxxx (ID channel/grup)
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
                return invite_info  # belum join
        except InviteHashExpiredError:
            raise ValueError("Link undangan sudah kadaluarsa.")
        except InviteHashInvalidError:
            raise ValueError("Link undangan tidak valid.")
        except Exception as e:
            raise ValueError(f"Gagal membaca invite link: {e}")

    # Bersihkan link biasa
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
        raise ValueError(f"Grup/channel bersifat privat atau Anda tidak punya akses: {t}")
    except Exception as e:
        raise ValueError(f"Gagal resolve target '{t}': {e}")


async def pastikan_join(client, entity):
    """
    Pastikan akun sudah bergabung ke grup tujuan. Jika belum, coba join
    (hanya jika grup publik atau sudah punya invite).
    """
    try:
        # Cek keanggotaan
        await client.get_permissions(entity, "me")
        return True, "sudah bergabung"
    except UserNotParticipantError:
        pass
    except Exception:
        pass

    # Coba join jika publik
    try:
        await client(functions.channels.JoinChannelRequest(entity))
        return True, "berhasil join"
    except Exception as e:
        return False, f"tidak bisa join otomatis: {e}"


async def ambil_anggota(client, entity):
    """
    Ambil daftar anggota yang bisa diproses.
    Return: list of (user_id, username, nama)
    """
    anggota = []
    try:
        async for user in client.iter_participants(entity):
            if user.bot:
                continue
            if user.deleted:
                continue
            username = f"@{user.username}" if user.username else None
            nama = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
            anggota.append((user.id, username, nama.strip() or "(tanpa nama)"))
    except ChatAdminRequiredError:
        raise ValueError("Anda bukan admin grup sumber, tidak bisa membaca daftar anggota.")
    except Exception as e:
        raise ValueError(f"Gagal mengambil anggota: {e}")
    return anggota


def simpan_log(lines, path=LOG_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n[✓] Log disimpan ke: {path}")
    except Exception as e:
        print(f"[!] Gagal menyimpan log: {e}")


def format_durasi(detik):
    m, s = divmod(int(detik), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}j {m}m {s}d"
    if m:
        return f"{m}m {s}d"
    return f"{s}d"


# ============================================================
# LOGIKA UTAMA
# ============================================================
async def proses_invite(client, grup_tujuan, anggota, dry_run=False):
    """
    Proses menambahkan anggota satu per satu.
    Return dict statistik.
    """
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

        if dry_run:
            print("(dry-run, dilewati)")
            stats["berhasil"] += 1
            stats["detail_berhasil"].append(f"[DRY-RUN] {label} ({nama})")
            continue

        try:
            user_entity = await client.get_entity(uid)
        except Exception as e:
            print(f"GAGAL (tidak bisa resolve user: {e})")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - resolve gagal: {e}")
            continue

        try:
            await client(InviteToChannelRequest(channel=grup_tujuan, users=[user_entity]))
            print("BERHASIL ditambahkan")
            stats["berhasil"] += 1
            stats["detail_berhasil"].append(f"{label} ({nama})")
        except UserAlreadyParticipantError:
            print("SUDAH ADA di grup tujuan")
            stats["sudah_ada"] += 1
            stats["detail_sudah_ada"].append(f"{label} ({nama})")
        except FloodWaitError as e:
            print(f"FloodWait {e.seconds}s, menunggu...")
            await asyncio.sleep(e.seconds + 2)
            # Coba ulang sekali
            try:
                await client(InviteToChannelRequest(channel=grup_tujuan, users=[user_entity]))
                print("  -> BERHASIL setelah tunggu")
                stats["berhasil"] += 1
                stats["detail_berhasil"].append(f"{label} ({nama})")
            except Exception as e2:
                print(f"  -> GAGAL setelah tunggu: {e2}")
                stats["gagal"] += 1
                stats["detail_gagal"].append(f"{label} ({nama}) - {e2}")
        except UserPrivacyRestrictedError:
            print("GAGAL (privasi user membatasi)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - privasi user")
        except UserNotMutualContactError:
            print("GAGAL (bukan kontak mutual)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - bukan kontak mutual")
        except UserChannelsTooMuchError:
            print("GAGAL (user sudah di terlalu banyak channel)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - channel terlalu banyak")
        except PeerFloodError:
            print("GAGAL (PeerFlood - akun Anda dibatasi Telegram)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - PeerFlood")
            print("\n[!] PeerFlood terdeteksi. Hentikan proses dan tunggu beberapa jam/hari.")
            break
        except ChatAdminRequiredError:
            print("GAGAL (butuh hak admin di grup tujuan)")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - butuh admin")
            break
        except Exception as e:
            print(f"GAGAL ({e})")
            stats["gagal"] += 1
            stats["detail_gagal"].append(f"{label} ({nama}) - {e}")

        # Jeda aman
        await asyncio.sleep(DELAY_BETWEEN_INVITES)

    return stats


async def main_async():
    clear_screen()
    header("MIGRASI ANGGOTA TELEGRAM (Telethon)")

    print("""
PERINGATAN:
- Script ini menambahkan anggota ke grup tujuan satu per satu.
- Jika Telegram membatasi, anggota akan dilewati & dicatat.
- Gunakan hanya untuk grup yang Anda kelola sendiri.
- Alternatif resmi: bagikan link undangan grup tujuan.
""")

    api_id, api_hash, phone = ambil_input_konfigurasi()

    os.makedirs(SESSION_DIR, exist_ok=True)
    session_path = os.path.join(SESSION_DIR, "migrasi")

    client = TelegramClient(session_path, api_id, api_hash)
    await client.start(phone=phone, password=lambda: getpass.getpass("Password 2FA (jika ada): "))

    me = await client.get_me()
    print(f"\n[✓] Login sebagai: {me.first_name} (@{me.username or 'tanpa username'})")

    # --- Input grup sumber & tujuan ---
    sumber_str = input("\nMasukkan username/link/ID grup SUMBER: ").strip()
    tujuan_str = input("Masukkan username/link/ID grup TUJUAN: ").strip()

    print("\n[·] Resolve grup sumber...")
    try:
        grup_sumber = await resolve_target(client, sumber_str)
    except ValueError as e:
        print(f"[!] {e}")
        return

    print("[·] Resolve grup tujuan...")
    try:
        grup_tujuan = await resolve_target(client, tujuan_str)
    except ValueError as e:
        print(f"[!] {e}")
        return

    print(f"[✓] Sumber: {getattr(grup_sumber, 'title', grup_sumber)}")
    print(f"[✓] Tujuan: {getattr(grup_tujuan, 'title', grup_tujuan)}")

    # Pastikan sudah join grup tujuan
    ok, msg = await pastikan_join(client, grup_tujuan)
    print(f"[i] Status keanggotaan grup tujuan: {msg}")
    if not ok:
        print("[!] Tidak bisa melanjutkan tanpa bergabung ke grup tujuan.")
        return

    # --- Ambil anggota ---
    print("\n[·] Mengambil daftar anggota grup sumber...")
    try:
        anggota = await ambil_anggota(client, grup_sumber)
    except ValueError as e:
        print(f"[!] {e}")
        return

    if not anggota:
        print("[!] Tidak ada anggota yang bisa diproses.")
        return

    print(f"\n[✓] Ditemukan {len(anggota)} anggota yang dapat diproses.")
    print("    (Bot, akun terhapus, dan akun tanpa akses sudah difilter otomatis)")

    # --- Dry run? ---
    dry = input("\nJalankan mode DRY-RUN (tidak menambahkan, hanya simulasi)? [y/n]: ").strip().lower()
    dry_run = dry == "y"

    # --- Konfirmasi ---
    konfirmasi = input("\nMulai proses pemindahan anggota? [y/n]: ").strip().lower()
    if konfirmasi != "y":
        print("[i] Dibatalkan oleh pengguna.")
        return

    # --- Proses ---
    header("MEMULAI PROSES")
    mulai = time.time()
    stats = await proses_invite(client, grup_tujuan, anggota, dry_run=dry_run)
    durasi = time.time() - mulai

    # --- Simpan log ---
    log_lines = []
    log_lines.append(f"# Hasil Migrasi Telegram - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_lines.append(f"# Sumber: {getattr(grup_sumber, 'title', sumber_str)}")
    log_lines.append(f"# Tujuan: {getattr(grup_tujuan, 'title', tujuan_str)}")
    log_lines.append(f"# Mode  : {'DRY-RUN' if dry_run else 'LIVE'}")
    log_lines.append("")
    log_lines.append(f"## BERHASIL ({len(stats['detail_berhasil'])})")
    log_lines.extend(stats["detail_berhasil"] or ["-"])
    log_lines.append("")
    log_lines.append(f"## SUDAH ADA ({len(stats['detail_sudah_ada'])})")
    log_lines.extend(stats["detail_sudah_ada"] or ["-"])
    log_lines.append("")
    log_lines.append(f"## GAGAL ({len(stats['detail_gagal'])})")
    log_lines.extend(stats["detail_gagal"] or ["-"])

    simpan = input("\nSimpan log ke hasil_migrasi.txt? [y/n]: ").strip().lower()
    if simpan == "y":
        simpan_log(log_lines)

    # --- Ringkasan ---
    header("RINGKASAN")
    print(f"Total anggota ditemukan : {len(anggota)}")
    print(f"Berhasil                : {stats['berhasil']}")
    print(f"Sudah ada               : {stats['sudah_ada']}")
    print(f"Gagal                   : {stats['gagal']}")
    print(f"Total waktu proses      : {format_durasi(durasi)}")
    if dry_run:
        print("\n[i] Mode DRY-RUN: tidak ada anggota yang benar-benar ditambahkan.")

    await client.disconnect()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[i] Dihentikan oleh pengguna.")
    except Exception as e:
        print(f"\n[!] Error fatal: {e}")


if __name__ == "__main__":
    main()
