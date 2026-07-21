from pathlib import Path
import re
import csv

import matplotlib
matplotlib.use("Qt5Agg")

import matplotlib.pyplot as plt


_OPEN_FIGURES = []


def _read_session_info(session_folder: Path) -> dict:
    info = {
        "datetime": session_folder.name,
        "participant": "Unknown participant",
        "session": "Unknown session",
        "comments": "",
    }

    info_file = session_folder / "session_info.txt"

    if not info_file.exists():
        return info

    text = info_file.read_text(encoding="utf-8", errors="replace")

    datetime_match = re.search(r"Время и дата начала сеанса:\s*(.*)", text)
    participant_match = re.search(r"Участник:\s*(.*)", text)
    session_match = re.search(r"Номер сеанса:\s*(.*)", text)

    if datetime_match:
        info["datetime"] = datetime_match.group(1).strip()

    if participant_match:
        info["participant"] = participant_match.group(1).strip()

    if session_match:
        info["session"] = session_match.group(1).strip()

    if "Комментарии:" in text:
        info["comments"] = text.split("Комментарии:", 1)[1].strip()

    return info


def _to_float(value):
    try:
        return float(value)
    except Exception:
        return None


def _load_emg_csv(path: Path):

    timestamps = []
    values = []

    with open(path, "r", encoding="utf-8", newline="") as f:

        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise ValueError("emg.csv has no header.")

        required = {
            "relative_time_s",
            "emg"
        }

        if not required.issubset(set(reader.fieldnames)):
            raise ValueError(
                "emg.csv must contain columns: relative_time_s, emg"
            )

        for row in reader:

            t = _to_float(row.get("relative_time_s"))
            v = _to_float(row.get("emg"))

            if t is None or v is None:
                continue

            timestamps.append(t)
            values.append(v)

    if not timestamps:
        raise ValueError("emg.csv contains no valid numeric data.")

    return timestamps, values

def _load_mcu_csv(path: Path):

    timestamps = []
    angles = []
    loads = []

    with open(path, "r", encoding="utf-8", newline="") as f:

        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise ValueError("mcu.csv has no header.")

        required = {
            "pc_timestamp_s",
            "angle_deg",
            "load_norm"
        }

        if not required.issubset(set(reader.fieldnames)):
            raise ValueError(
                "mcu.csv must contain columns: pc_timestamp_s, angle_deg, load_norm"
            )

        for row in reader:

            t = _to_float(row.get("pc_timestamp_s"))
            a = _to_float(row.get("angle_deg"))
            l = _to_float(row.get("load_norm"))

            if t is None or a is None or l is None:
                continue

            timestamps.append(t)
            angles.append(a)
            loads.append(l)

    if not timestamps:
        raise ValueError("mcu.csv contains no valid numeric data.")

    # convert PC absolute timestamps to relative session time
    t0 = timestamps[0]
    timestamps = [t - t0 for t in timestamps]

    return timestamps, angles, loads


def _filter_by_time(t_values, *y_values, t_min=0.0, t_max=None):
    """
    Keep only samples inside shared x-axis range.
    This does not modify CSV files; it only affects plotting.
    """

    if t_max is None:
        return t_values, *y_values

    filtered_t = []
    filtered_y = [[] for _ in y_values]

    for i, t in enumerate(t_values):
        if t < t_min or t > t_max:
            continue

        filtered_t.append(t)

        for j, y in enumerate(y_values):
            filtered_y[j].append(y[i])

    return filtered_t, *filtered_y


def plot_session_folder(session_folder):
    session_folder = Path(session_folder)

    if not session_folder.exists():
        raise FileNotFoundError(f"Session folder does not exist: {session_folder}")

    if not session_folder.is_dir():
        raise NotADirectoryError(f"Selected path is not a folder: {session_folder}")

    emg_file = session_folder / "emg.csv"
    mcu_file = session_folder / "mcu.csv"

    if not emg_file.exists():
        raise FileNotFoundError(f"Missing file: {emg_file}")

    if not mcu_file.exists():
        raise FileNotFoundError(f"Missing file: {mcu_file}")

    info = _read_session_info(session_folder)

    # Use real CSV timestamps directly.
    emg_t, emg_v = _load_emg_csv(emg_file)
    mcu_t, angle_v, load_v = _load_mcu_csv(mcu_file)

    # Shared common time range.
    # Start from the later first timestamp, end at the earlier last timestamp.
    shared_start = max(min(emg_t), min(mcu_t))
    shared_end = min(max(emg_t), max(mcu_t))

    if shared_end <= shared_start:
        raise ValueError(
            "EMG and MCU time ranges do not overlap enough to plot together."
        )

    # Keep only the overlapping part of both streams.
    emg_t_plot, emg_v_plot = _filter_by_time(
        emg_t,
        emg_v,
        t_min=shared_start,
        t_max=shared_end,
    )

    mcu_t_plot, angle_v_plot, load_v_plot = _filter_by_time(
        mcu_t,
        angle_v,
        load_v,
        t_min=shared_start,
        t_max=shared_end,
    )

    # Shift both streams so the common visible range starts at 0.
    emg_t_plot = [t - shared_start for t in emg_t_plot]
    mcu_t_plot = [t - shared_start for t in mcu_t_plot]

    shared_xmin = 0.0
    shared_xmax = shared_end - shared_start

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 9))

    fig.canvas.manager.set_window_title(
        f"{info['participant']} | {info['session']} | {info['datetime']}"
    )

    main_title = (
        f"Участник: {info['participant']}    "
        f"Сеанс: {info['session']}    "
        f"Дата: {info['datetime']}"
    )

    fig.suptitle(main_title, fontsize=12)

    # =====================================================
    # EMG PLOT
    # =====================================================

    ax1.plot(
        emg_t_plot,
        emg_v_plot,
        linewidth=1,
        color="blue",
    )

    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("EMG")
    ax1.set_title("EMG Signal Over Time")
    ax1.grid(True, alpha=0.3)
    ax1.set_xlim(shared_xmin, shared_xmax)

    # =====================================================
    # MCU PLOT
    # =====================================================

    ax2_angle = ax2
    ax2_load = ax2_angle.twinx()

    ax2_angle.plot(
        mcu_t_plot,
        angle_v_plot,
        linewidth=2,
        color="green",
        label="Angle",
    )

    ax2_load.plot(
        mcu_t_plot,
        load_v_plot,
        linewidth=2,
        color="red",
        label="Load",
    )

    ax2_angle.set_xlabel("Time (s)")
    ax2_angle.set_ylabel("Angle (deg)", color="green")
    ax2_load.set_ylabel("Load", color="red")

    ax2_angle.tick_params(axis="y", labelcolor="green")
    ax2_load.tick_params(axis="y", labelcolor="red")

    ax2_angle.set_title("MCU Data: Angle and Load")
    ax2_angle.grid(True, alpha=0.3)
    ax2_angle.set_xlim(shared_xmin, shared_xmax)

    lines1, labels1 = ax2_angle.get_legend_handles_labels()
    lines2, labels2 = ax2_load.get_legend_handles_labels()

    ax2_angle.legend(
        lines1 + lines2,
        labels1 + labels2,
        loc="upper right",
    )

    if info["comments"]:
        comment_text = info["comments"]

        if len(comment_text) > 300:
            comment_text = comment_text[:300] + "..."

        fig.text(
            0.01,
            0.01,
            f"Комментарии: {comment_text}",
            fontsize=9,
            ha="left",
            va="bottom",
        )

    plt.tight_layout(rect=[0, 0.04, 1, 0.95])

    _OPEN_FIGURES.append(fig)

    plt.show(block=False)

    return fig