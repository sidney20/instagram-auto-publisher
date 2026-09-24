from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from core.logger import get_logger


@dataclass
class CoverResult:
    found: bool
    frame: int = 0
    time_seconds: float = 0.0
    confidence: float = 0.0
    is_first_frame: bool = False
    total_frames: int = 0
    fps: float = 0.0
    duration: float = 0.0
    start_frame: int = 0
    end_frame: int = 0


def extract_frame(video_path: str | Path, frame_index: int) -> np.ndarray | None:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_index)))
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return None
    return frame


def _cluster_candidates(candidates: list[tuple[int, float]], gap: int) -> list[list[tuple[int, float]]]:
    if not candidates:
        return []
    clusters: list[list[tuple[int, float]]] = [[candidates[0]]]
    for cand in candidates[1:]:
        if cand[0] - clusters[-1][-1][0] <= gap:
            clusters[-1].append(cand)
        else:
            clusters.append([cand])
    return clusters


def _log(msg: str) -> None:
    get_logger().info(f"[CAPA] {msg}")


def _banner_signature(frame: np.ndarray) -> dict:
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame

    edges = cv2.Canny(gray, 50, 150)
    edge_density = float(np.count_nonzero(edges)) / (h * w)

    bottom = gray[int(h * 0.6):, :]
    top = gray[:int(h * 0.4), :]
    contrast = abs(float(np.mean(bottom)) - float(np.mean(top)))

    h_proj = cv2.reduce(bottom, 1, cv2.REDUCE_AVG).flatten()
    line_std = float(np.std(h_proj))

    half_width = w // 2
    mid_left = gray[int(h * 0.3):int(h * 0.7), :half_width]
    mid_right = gray[int(h * 0.3):int(h * 0.7), w - half_width:]
    symmetry = 1.0 - (float(np.mean(np.abs(mid_left.astype(float) - mid_right.astype(float)))) / 255.0)

    color_std = 0.0
    if len(frame.shape) == 3:
        for ch in range(3):
            ch_std = float(np.std(frame[:, :, ch]))
            color_std = max(color_std, ch_std)

    return {
        "edge_density": edge_density,
        "contrast": contrast,
        "line_std": line_std,
        "symmetry": symmetry,
        "color_std": color_std,
    }


def _compute_banner_score(sig: dict) -> float:
    score = 0.0

    if sig["edge_density"] > 0.04:
        score += 0.25
    elif sig["edge_density"] > 0.025:
        score += 0.10

    if sig["contrast"] > 30:
        score += 0.25
    elif sig["contrast"] > 18:
        score += 0.10

    if sig["line_std"] > 12:
        score += 0.20
    elif sig["line_std"] > 7:
        score += 0.08

    if sig["symmetry"] > 0.75:
        score += 0.15
    elif sig["symmetry"] > 0.60:
        score += 0.07

    if sig["color_std"] > 40:
        score += 0.15
    elif sig["color_std"] > 25:
        score += 0.07

    return min(score, 1.0)


def _is_banner_frame(frame: np.ndarray, ref_sig: dict | None = None) -> tuple[bool, float]:
    sig = _banner_signature(frame)
    score = _compute_banner_score(sig)

    if ref_sig is not None:
        ref_score = _compute_banner_score(ref_sig)
        score_diff = abs(score - ref_score)
        if score_diff < 0.05 and score < 0.6:
            return False, 0.0

    return score >= 0.45, score


def _small_gray(frame: np.ndarray, size: int = 16) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    return cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)


def _novelty(prev: np.ndarray, cur: np.ndarray) -> float:
    if prev is None:
        return 0.0
    diff = np.abs(prev.astype(np.float32) - cur.astype(np.float32))
    return float(np.mean(diff) / 255.0)


def find_cover_frame(video_path: str | Path) -> CoverResult:
    _log(f"Analisando vídeo: {video_path}")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        _log("ERRO: não foi possível abrir o vídeo")
        return CoverResult(found=False)

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if fps <= 0:
        fps = 30.0
    duration = total_frames / fps if fps > 0 else 0.0

    _log(f"FPS: {fps:.1f}")
    _log(f"Total de frames: {total_frames}")
    _log(f"Resolução: {width}x{height}")
    _log(f"Duração: {duration:.2f}s")

    ret, first_frame = cap.read()
    if not ret:
        cap.release()
        _log("ERRO: não foi possível ler o primeiro frame")
        return CoverResult(found=False, total_frames=total_frames, fps=fps, duration=duration)

    _log("Verificando primeiro frame...")
    first_sig = _banner_signature(first_frame)
    is_first, conf_first = _is_banner_frame(first_frame)
    if is_first and conf_first >= 0.65:
        _log(f"Primeiro frame É o banner! (confiança: {conf_first:.2f})")
        cap.release()
        return CoverResult(
            found=True, frame=0, time_seconds=0.0, confidence=conf_first,
            is_first_frame=True, total_frames=total_frames, fps=fps, duration=duration,
            start_frame=0, end_frame=0,
        )

    _log("Primeiro frame não é o banner. Procurando...")

    coarse_step = max(1, int(fps * 0.25))
    coarse_candidates = []
    frame_idx = 0
    prev_small = None

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % coarse_step == 0 and frame_idx > 0:
            disp = frame
            if width > 480 or height > 480:
                disp = cv2.resize(
                    frame,
                    (int(width * 480 / max(width, height)), 480),
                    interpolation=cv2.INTER_AREA,
                )
            small = _small_gray(disp)
            novelty = _novelty(prev_small, small)
            prev_small = small

            is_banner, conf = _is_banner_frame(frame, ref_sig=first_sig)
            is_candidate = (
                conf >= 0.40
                and (conf >= 0.55 or novelty >= 0.30)
            )
            if is_banner or is_candidate:
                coarse_candidates.append((frame_idx, conf, is_banner, novelty))
                _log(
                    f"Candidato: frame {frame_idx} (conf={conf:.2f}, "
                    f"banner={is_banner}, novelty={novelty:.2f})"
                )
        elif frame_idx == 0:
            disp = frame
            if width > 480 or height > 480:
                disp = cv2.resize(
                    frame,
                    (int(width * 480 / max(width, height)), 480),
                    interpolation=cv2.INTER_AREA,
                )
            prev_small = _small_gray(disp)

        frame_idx += 1

    if not coarse_candidates:
        _log("Banner não encontrado (busca coarse)")
        cap.release()
        return CoverResult(found=False, total_frames=total_frames, fps=fps, duration=duration)

    _log(f"Coarse: {len(coarse_candidates)} candidato(s)")

    gap = max(2, int(fps))
    clusters = _cluster_candidates(coarse_candidates, gap=gap)

    for ci, cl in enumerate(clusters):
        cl_frames = [c[0] for c in cl]
        cl_conf = sum(c[1] for c in cl) / len(cl)
        _log(
            f"Cluster {ci + 1}: frames {cl_frames[0]}-{cl_frames[-1]} "
            f"({len(cl)} candidato(s), conf média {cl_conf:.2f})"
        )

    best_cluster = max(
        clusters,
        key=lambda cl: (sum(1 for c in cl if c[2]), sum(c[1] for c in cl) / len(cl)),
    )
    best_coarse_frame = best_cluster[0][0]

    region_start = max(1, best_coarse_frame - coarse_step)
    region_end = min(total_frames - 1, best_cluster[-1][0] + coarse_step)

    _log(f"Refinando região: frames {region_start}-{region_end}...")

    banner_frames = []
    cap.set(cv2.CAP_PROP_POS_FRAMES, region_start)
    for f in range(region_start, region_end + 1):
        ret, frame = cap.read()
        if not ret:
            break
        is_banner, conf = _is_banner_frame(frame, ref_sig=first_sig)
        if conf >= 0.35:
            banner_frames.append((f, conf, is_banner))

    cap.release()

    if not banner_frames:
        _log("Nenhum frame de banner confirmado no refino")
        return CoverResult(found=False, total_frames=total_frames, fps=fps, duration=duration)

    start_frame = banner_frames[0][0]
    end_frame = banner_frames[-1][0]
    best_item = max(banner_frames, key=lambda c: c[1])
    best_frame = best_item[0]
    best_conf = best_item[1]
    mid_frame = (start_frame + end_frame) // 2

    chosen_frame = mid_frame
    time_sec = chosen_frame / fps

    _log(f"Região do banner: frames {start_frame}-{end_frame} ({end_frame - start_frame + 1} frames)")
    _log(f"Frame escolhido: {chosen_frame} (centro da região)")
    _log(f"Tempo aproximado: {time_sec:.2f}s")

    return CoverResult(
        found=True,
        frame=chosen_frame,
        time_seconds=time_sec,
        confidence=best_conf,
        is_first_frame=False,
        total_frames=total_frames,
        fps=fps,
        duration=duration,
        start_frame=start_frame,
        end_frame=end_frame,
    )
