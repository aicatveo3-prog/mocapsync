# -*- coding: utf-8 -*-
"""
.trc (OpenSim 마커 궤적) -> Blender 애니메이션.

Blender 안에서 실행하는 스크립트입니다. (bpy 필요)

좌표계
------
.trc 는 Y-up (실측으로 확인: 머리-발 평균차가 Y축에서 +1.42m).
Blender 는 Z-up. 오른손계를 유지하는 변환은:

    blender_x =  trc_x
    blender_y = -trc_z
    blender_z =  trc_y

행렬식 det([[1,0,0],[0,0,-1],[0,1,0]]) = +1 이므로 좌우가 뒤집히지 않습니다.
(이게 뒤집히면 왼발/오른발이 바뀌어서 모션캡쳐 결과가 조용히 틀립니다)

구조
----
- 마커: 마커당 구(sphere) 1개. 프레임마다 location 키프레임.
- 뼈: 실린더 1개 + [Copy Location -> 부모마커] + [Stretch To -> 자식마커].
  Stretch To 는 오브젝트의 로컬 Y축을 타깃으로 늘리므로,
  실린더 메시를 "원점에서 시작해 +Y 로 길이 1" 형태로 만들어 둡니다.
  키프레임을 뼈에는 하나도 안 넣어도 마커를 따라 자동으로 움직입니다.

사용법 (Blender Python Console 또는 MCP)
---------------------------------------
    import sys, importlib
    sys.path.insert(0, r"C:\\...\\모션캡쳐_Pose2Sim\\tools")
    import trc_to_blender; importlib.reload(trc_to_blender)
    trc_to_blender.build(r"C:\\Users\\USER\\Pose2SimWork\\...\\xxx.trc")
"""

from __future__ import annotations

import math
import os

import bpy  # type: ignore
import bmesh  # type: ignore
from mathutils import Matrix, Vector  # type: ignore


COLLECTION_NAME = "MocapSync_TRC"

# HALPE_26 기반 골격 연결 (Pose2Sim 이 삼각측량하는 22 마커)
BONES = [
    ("Hip", "RHip"), ("RHip", "RKnee"), ("RKnee", "RAnkle"),
    ("RAnkle", "RHeel"), ("RAnkle", "RBigToe"), ("RBigToe", "RSmallToe"),
    ("Hip", "LHip"), ("LHip", "LKnee"), ("LKnee", "LAnkle"),
    ("LAnkle", "LHeel"), ("LAnkle", "LBigToe"), ("LBigToe", "LSmallToe"),
    ("Hip", "Neck"), ("Neck", "Head"), ("Head", "Nose"),
    ("Neck", "RShoulder"), ("RShoulder", "RElbow"), ("RElbow", "RWrist"),
    ("Neck", "LShoulder"), ("LShoulder", "LElbow"), ("LElbow", "LWrist"),
]

# 좌/우를 색으로 구분합니다. 좌우가 뒤집혔는지 눈으로 바로 확인하려는 목적.
COL_RIGHT = (0.95, 0.30, 0.25, 1.0)   # 빨강 = 오른쪽
COL_LEFT = (0.25, 0.55, 0.95, 1.0)    # 파랑 = 왼쪽
COL_CENTER = (0.95, 0.95, 0.95, 1.0)  # 흰색 = 중앙


# ─────────────────────────────────────────────────────────────────────────────
# TRC 파싱 (Blender 내장 파이썬에는 외부 의존성이 없으므로 여기 자체 구현)
# ─────────────────────────────────────────────────────────────────────────────
def load_trc(path: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    if len(lines) < 6:
        raise ValueError("TRC 가 너무 짧습니다: " + path)

    keys = [k.strip() for k in lines[1].split("\t")]
    vals = [v.strip() for v in lines[2].split("\t")]
    header = dict(zip(keys, vals))
    markers = [m.strip() for m in lines[3].split("\t")[2:] if m.strip()]

    def num(tok):
        tok = tok.strip()
        if not tok:
            return float("nan")
        try:
            return float(tok)
        except ValueError:
            return float("nan")

    frames = []
    times = []
    for ln in lines[5:]:
        if not ln.strip():
            continue
        p = ln.split("\t")
        if len(p) < 3:
            continue
        times.append(num(p[1]))
        c = [num(v) for v in p[2:]]
        pts = []
        for k in range(len(markers)):
            i = 3 * k
            pts.append((c[i], c[i + 1], c[i + 2]) if i + 2 < len(c)
                       else (float("nan"),) * 3)
        frames.append(pts)

    return {"header": header, "markers": markers, "frames": frames,
            "times": times, "name": os.path.basename(path)}


def trc_to_blender(p) -> Vector:
    """Y-up -> Z-up. 오른손계 유지."""
    return Vector((p[0], -p[2], p[1]))


# ─────────────────────────────────────────────────────────────────────────────
# 메시 / 머티리얼
# ─────────────────────────────────────────────────────────────────────────────
def _sphere_mesh(name: str, radius: float):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, radius=radius)
    except TypeError:
        # 구버전 API 호환
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, diameter=radius)
    bm.to_mesh(me)
    bm.free()
    # bmesh.ops.faces_shade_smooth 는 Blender 5.x 에 없습니다. 메시 플래그로 처리.
    for poly in me.polygons:
        poly.use_smooth = True
    return me


def _bone_mesh(name: str, radius: float):
    """원점에서 시작해 +Y 방향으로 길이 1인 실린더. Stretch To 가 Y축을 늘립니다."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=10,
                              radius1=radius, radius2=radius, depth=1.0)
    except TypeError:
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=10,
                              diameter1=radius, diameter2=radius, depth=1.0)
    # 현재: Z축 중심 -0.5..+0.5  ->  목표: +Y 축 0..+1
    bmesh.ops.translate(bm, verts=bm.verts, vec=(0.0, 0.0, 0.5))
    bmesh.ops.rotate(bm, verts=bm.verts, cent=(0.0, 0.0, 0.0),
                     matrix=Matrix.Rotation(math.radians(-90.0), 3, "X"))
    bm.to_mesh(me)
    bm.free()
    return me


def _material(name: str, rgba, emission: float = 0.0):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    if bsdf is None:
        for n in nt.nodes:
            if n.type == "BSDF_PRINCIPLED":
                bsdf = n
                break
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = rgba
        for key, val in (("Roughness", 0.45), ("Metallic", 0.0)):
            if key in bsdf.inputs:
                bsdf.inputs[key].default_value = val
        if emission > 0.0:
            if "Emission Color" in bsdf.inputs:
                bsdf.inputs["Emission Color"].default_value = rgba
            elif "Emission" in bsdf.inputs:
                bsdf.inputs["Emission"].default_value = rgba
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = emission
    mat.diffuse_color = rgba  # 솔리드 뷰포트 색
    return mat


def _side_color(marker: str):
    if marker.startswith("R"):
        return COL_RIGHT, "R"
    if marker.startswith("L"):
        return COL_LEFT, "L"
    return COL_CENTER, "C"


# ─────────────────────────────────────────────────────────────────────────────
# 정리
# ─────────────────────────────────────────────────────────────────────────────
def clear_previous():
    """이전에 이 스크립트가 만든 것만 지웁니다. 사용자 작업물은 건드리지 않습니다."""
    coll = bpy.data.collections.get(COLLECTION_NAME)
    if coll is None:
        return 0
    n = 0
    for ob in list(coll.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
        n += 1
    for sc in bpy.data.scenes:
        if coll.name in sc.collection.children:
            sc.collection.children.unlink(coll)
    bpy.data.collections.remove(coll)
    return n


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────
def build(trc_path: str,
          marker_radius: float = 0.022,
          bone_radius: float = 0.012,
          add_floor: bool = True,
          add_camera: bool = True,
          add_trajectory: str = "Hip",
          frame_offset: int = 1) -> dict:
    """
    .trc 를 읽어 Blender 씬에 애니메이션을 만듭니다.
    반환: 요약 dict (호출자가 로그로 찍을 수 있게)
    """
    trc = load_trc(trc_path)
    markers = trc["markers"]
    frames = trc["frames"]
    nframes = len(frames)
    fps = float(trc["header"].get("DataRate") or 60)

    removed = clear_previous()

    coll = bpy.data.collections.new(COLLECTION_NAME)
    bpy.context.scene.collection.children.link(coll)

    # 마커 위치를 Blender 좌표로 미리 변환 + NaN 은 마지막 유효값 유지
    conv = []   # conv[frame][marker] = Vector
    last = [None] * len(markers)
    for pts in frames:
        row = []
        for k, p in enumerate(pts):
            if any(math.isnan(c) for c in p):
                row.append(last[k] if last[k] is not None else Vector((0, 0, 0)))
            else:
                v = trc_to_blender(p)
                last[k] = v
                row.append(v)
        conv.append(row)

    # ── 마커 ──
    sphere_me = _sphere_mesh("MS_marker_sphere", marker_radius)
    mats = {
        "R": _material("MS_right", COL_RIGHT, emission=0.6),
        "L": _material("MS_left", COL_LEFT, emission=0.6),
        "C": _material("MS_center", COL_CENTER, emission=0.4),
    }
    marker_objs = {}
    for k, name in enumerate(markers):
        ob = bpy.data.objects.new("M_" + name, sphere_me)
        rgba, side = _side_color(name)
        ob.data = sphere_me                    # 메시 공유 (가볍게)
        ob.color = rgba
        coll.objects.link(ob)
        if not ob.data.materials:
            ob.data.materials.append(mats["C"])
        # 오브젝트 단위로 머티리얼을 다르게 주려면 슬롯을 오브젝트에 링크
        ob.material_slots[0].link = "OBJECT"
        ob.material_slots[0].material = mats[side]
        marker_objs[name] = ob

        # 키프레임: 프레임마다 location.
        # fcurve 를 직접 만들지 않고 keyframe_insert 를 쓰는 이유 —
        # Blender 4.4+ 의 slotted actions 때문에 action.fcurves 직접 접근이
        # 버전마다 다릅니다. keyframe_insert 는 내부에서 알아서 처리합니다.
        for fi in range(nframes):
            ob.location = conv[fi][k]
            ob.keyframe_insert(data_path="location", frame=frame_offset + fi)

    # ── 뼈 ──
    bone_me = _bone_mesh("MS_bone_cyl", bone_radius)
    bone_mat = _material("MS_bone", (0.80, 0.80, 0.84, 1.0))
    made_bones = 0
    skipped = []
    for a, b in BONES:
        if a not in marker_objs or b not in marker_objs:
            skipped.append(f"{a}-{b}")
            continue
        ob = bpy.data.objects.new(f"B_{a}_{b}", bone_me)
        coll.objects.link(ob)
        if not ob.data.materials:
            ob.data.materials.append(bone_mat)

        c1 = ob.constraints.new("COPY_LOCATION")
        c1.target = marker_objs[a]
        c2 = ob.constraints.new("STRETCH_TO")
        c2.target = marker_objs[b]
        c2.rest_length = 1.0          # 메시 길이가 1 이므로
        c2.volume = "NO_VOLUME"       # 늘어날 때 굵기 유지
        made_bones += 1

    # ── 골반 궤적 곡선 ──
    traj_obj = None
    if add_trajectory and add_trajectory in markers:
        ti = markers.index(add_trajectory)
        cu = bpy.data.curves.new("MS_trajectory", type="CURVE")
        cu.dimensions = "3D"
        cu.bevel_depth = 0.004
        sp = cu.splines.new("POLY")
        sp.points.add(nframes - 1)
        for fi in range(nframes):
            v = conv[fi][ti]
            sp.points[fi].co = (v.x, v.y, v.z, 1.0)
        traj_obj = bpy.data.objects.new("MS_trajectory_" + add_trajectory, cu)
        coll.objects.link(traj_obj)
        tm = _material("MS_traj", (1.0, 0.75, 0.15, 1.0), emission=0.8)
        traj_obj.data.materials.append(tm)

    # ── 바닥 ──
    if add_floor:
        xs = [v.x for row in conv for v in row]
        ys = [v.y for row in conv for v in row]
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        size = max(max(xs) - min(xs), max(ys) - min(ys)) * 1.6 + 1.0
        me = bpy.data.meshes.new("MS_floor")
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=size / 2.0)
        bm.to_mesh(me)
        bm.free()
        floor = bpy.data.objects.new("MS_floor", me)
        floor.location = (cx, cy, 0.0)
        coll.objects.link(floor)
        floor.data.materials.append(_material("MS_floor_mat", (0.12, 0.13, 0.15, 1.0)))

    # ── 씬 설정 ──
    scene = bpy.context.scene
    scene.render.fps = int(round(fps))
    scene.render.fps_base = 1.0
    scene.frame_start = frame_offset
    scene.frame_end = frame_offset + nframes - 1
    scene.frame_set(frame_offset + nframes // 2)  # 중간 포즈로 이동

    # ── 카메라 ──
    cam_obj = None
    if add_camera:
        allv = [v for row in conv for v in row]
        cx = sum(v.x for v in allv) / len(allv)
        cy = sum(v.y for v in allv) / len(allv)
        cz = sum(v.z for v in allv) / len(allv)
        center = Vector((cx, cy, cz))

        cam_data = bpy.data.cameras.new("MS_Camera")
        cam_data.lens = 40.0
        cam_obj = bpy.data.objects.new("MS_Camera", cam_data)
        coll.objects.link(cam_obj)
        cam_obj.location = center + Vector((2.6, -3.6, 1.3))
        direction = center - cam_obj.location
        cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        scene.camera = cam_obj

    # ── 조명 (기존 조명이 없거나 어두울 때를 대비) ──
    if not any(o.type == "LIGHT" for o in bpy.data.objects):
        ld = bpy.data.lights.new("MS_Sun", type="SUN")
        ld.energy = 3.0
        lo = bpy.data.objects.new("MS_Sun", ld)
        lo.rotation_euler = (math.radians(50), 0.0, math.radians(30))
        coll.objects.link(lo)

    # ── 뷰포트를 보기 좋게 ──
    try:
        for area in bpy.context.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                space.shading.type = "MATERIAL"
                if add_camera:
                    space.region_3d.view_perspective = "CAMERA"
    except Exception:
        pass

    return {
        "file": trc["name"],
        "markers": len(markers),
        "frames": nframes,
        "fps": fps,
        "bones": made_bones,
        "bones_skipped": skipped,
        "removed_previous": removed,
        "frame_range": (scene.frame_start, scene.frame_end),
        "collection": COLLECTION_NAME,
    }
