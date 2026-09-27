"""Модель подъёмной машины для сценариев: исправное состояние, рейс, эпизоды.

Имитатор не исполняет программу ПЛК. Поэтому сценарий задаёт и причину
(изменение входа), и её физические последствия на других входах: отпадание
реле ТП, выбег привода, остановку энкодеров, действия машиниста. Цепочки
следуют логике защиты ТП проекта: какой вход считается нормой и что
срабатывает следом, выведено из программы ПЛК, а не придумано. Там, где
полярность входа из программы однозначно не следует, значение вынесено в
``ASSUMPTIONS`` и должно быть подтверждено инженером.

Всё здесь чистое: время — миллисекунды от начала цикла, случайность — только
переданный ``random.Random``. Один seed — одна и та же шкала изменений.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .timeline import TICK_MS, Change, Timeline, wrap_int16

# ---------------------------------------------------------------------------
# Сигналы (идентификаторы из docs/opcua_input_bindings.json)
# ---------------------------------------------------------------------------

ESTOP_CONSOLE = "pu-s1-avstop"  # аварийный стоп ПМ, контакт нормально замкнут
ESTOP_CONSOLE_2 = "pu-s31-avstop"
DRIVE_EMERGENCY_OFF = "pu-s9-fltpch"  # аварийное отключение ПЧ, нормально замкнут
KAR_ZERO = "pu-u1-zero"
KAR_UP = "pu-u1-up"
KAR_DOWN = "pu-u1-down"
KRT_BRAKED = "pu-u2-braked"
KRT_RELEASED = "pu-u2-released"
ACKNOWLEDGE = "pu-s22-ack"
RESET_TP = "pu-s21-reset-tp"
TP_RELAY = "ch1-10a3-k1"  # контроль общего реле аварии ТП: 1 — реле под током

SYNC_LEFT = ("ch1-10a3-sq1", "ch2-20a3-sq1")
SYNC_RIGHT = ("ch1-10a3-sq3", "ch2-20a3-sq3")
ENCODERS = ("ch1-encoder-1", "ch1-encoder-2", "ch2-encoder-1", "ch2-encoder-2")

MOTOR_SPEED = "profibus-motor-speed"
MOTOR_CURRENT = "profibus-current-filtered"
MOTOR_TORQUE = "profibus-output-torque"
INV_STATUS = "profibus-inv-status-word"
INV_STATUS_1 = "profibus-inv-status-word-1"
INV_STATUS_2 = "profibus-inv-status-word-2"
REC_STATUS = "profibus-rec-status-word"
INV_FAULT_ID = "profibus-inv-fault-id"

# Биты слов состояния — порядок полей структур в типах данных проекта.
INV_PREPARE = 1 << 0
INV_FUNCTION = 1 << 1
INV_NORMAL_OPER = 1 << 5
INV_FAULT = 1 << 7

SW1_CONN_PREPARED = 1 << 0
SW1_OPER_PREPARED = 1 << 1
SW1_XFUNCTION = 1 << 2
SW1_FAULT = 1 << 3
SW1_OFF2_INACTIVE = 1 << 4
SW1_OFF3_INACTIVE = 1 << 5
SW1_ALARM = 1 << 7
SW1_SPEED_REACHED = 1 << 8
SW1_PLC_CONTROL = 1 << 9
SW1_RELEASE_HOLD_BRAKE = 1 << 12
SW1_NO_MOTOR_OVERTEMP = 1 << 13
SW1_ROTATION_DIR = 1 << 14
SW1_NO_POWER_OVERLOAD = 1 << 15

SW2_RAMP_ACTIVE = 1 << 0

REC_READY = 1 << 1
REC_IN_OPERATION = 1 << 2
REC_PRECHARGED = 1 << 11
REC_MAIN_CONTACTOR = 1 << 12

# ---------------------------------------------------------------------------
# Исправное состояние: машина стоит, заторможена, левый сосуд вверху
# ---------------------------------------------------------------------------

BASELINE: Dict[str, Any] = {
    # Канал 1: датчики ствола, контроль питания и реле ТП.
    "ch1-10a3-sq1": True,  # левый сосуд в зоне синхронизации вверху
    "ch1-10a3-sq3": False,
    "ch1-10a3-sq5": True,  # переподъём: вход датчика 1 — норма
    "ch1-10a3-sq6": True,
    "ch1-10a3-sprut-block": True,  # 0 — запрет пуска от тормозной системы
    "ch1-10a3-u20": True,  # контроль БРИЗ: 0 — авария
    "ch1-10a3-u21": True,
    "ch1-10a3-u22": True,
    TP_RELAY: True,
    "ch1-10a3-k2": False,
    "ch1-10a3-k200": True,  # 0 — авария канала 2
    "ch1-10a3-sq11": False,  # провисание каната: 1 — провисание
    "ch1-10a3-sq12": False,
    "ch1-10a4-u23-no": False,
    "ch1-10a4-u23-nc": True,  # контроль изоляции, контакт NC: 0 — низкая изоляция
    "ch1-10a4-uz1": True,  # контроль блоков питания: 0 — авария
    "ch1-10a4-uz2": True,
    "ch1-encoder-1": 0,
    "ch1-encoder-2": 0,
    # Канал 2.
    "ch2-20a3-sq1": True,
    "ch2-20a3-sq3": False,
    "ch2-20a3-sq5": False,  # в канале 2 вход датчика переподъёма инвертирован
    "ch2-20a3-sq6": False,
    "ch2-20a3-u10": True,
    "ch2-20a3-u11": True,
    "ch2-20a3-u12": True,
    "ch2-20a3-k2": False,
    "ch2-encoder-1": 0,
    "ch2-encoder-2": 0,
    # Пульт управления: рукоятки в нуле и «заторможено», кнопки отпущены.
    KAR_ZERO: True,
    KAR_UP: False,
    KAR_DOWN: False,
    "pu-sa10-comp1": True,
    "pu-sa10-off": False,
    "pu-sa10-comp2": False,
    "pu-s2": False,
    "pu-s3": False,
    "pu-s4": False,
    "pu-s5": False,
    "pu-s6": False,
    "pu-s7": False,
    "pu-s8": False,
    ESTOP_CONSOLE: True,
    DRIVE_EMERGENCY_OFF: True,
    KRT_BRAKED: True,
    KRT_RELEASED: False,
    "pu-sa25-manual": False,
    "pu-sa25-auto": True,
    "pu-sa26-test": False,
    "pu-sa27-bypass": False,
    "pu-sa28-off": False,
    "pu-sa28-on": False,
    RESET_TP: False,
    ACKNOWLEDGE: False,
    "pu-sa11-bypass": False,
    "pu-sa12-perest": False,
    "pu-sa13-left": True,
    "pu-sa13-mikon": False,
    "pu-sa13-right": False,
    ESTOP_CONSOLE_2: True,
    "pu-uz10": True,
    # Шкаф ввода питания: автоматы включены, контроль сети в норме.
    "shvp-qf1": True,
    "shvp-qf2": True,
    "shvp-qf9": True,
    "shvp-qf10": True,
    "shvp-qf11": True,
    "shvp-qf12": True,
    "shvp-qf14": True,
    "shvp-qf16": True,
    "shvp-qf17": True,
    "shvp-uz1": True,
    "shvp-u7-no": False,
    "shvp-u7-nc": True,  # контроль изоляции NC: 0 — низкая изоляция
    "shvp-u6": True,  # контроль напряжения и фаз: 0 — авария сети
    "shvp-k86": False,
    "shvp-k87": False,
    "shvp-qf24": True,
    "shvp-qf28": True,
    "shvp-qf29": True,
    "shvp-u50": True,
    "shvp-qf32": True,
    "shvp-qf33": True,
    "shvp-qf41": True,
    "shvp-qf42": True,
    "shvp-u51": True,
    "shvp-k1": False,  # разрешение включения ПМ требует 0 на этом входе
    "shvp-k2": False,
    "shvp-k3": False,
    "shvp-k4": False,
    "shvp-k6": True,  # выбран и работает компрессор 1
    "shvp-k7": False,
    # Привод: готов, не в работе, ошибок нет.
    INV_STATUS: INV_PREPARE,
    INV_STATUS_1: (
        SW1_CONN_PREPARED
        | SW1_OPER_PREPARED
        | SW1_OFF2_INACTIVE
        | SW1_OFF3_INACTIVE
        | SW1_PLC_CONTROL
        | SW1_NO_MOTOR_OVERTEMP
        | SW1_NO_POWER_OVERLOAD
    ),
    INV_STATUS_2: 0,
    REC_STATUS: REC_READY | REC_IN_OPERATION | REC_PRECHARGED | REC_MAIN_CONTACTOR,
    MOTOR_SPEED: 0,
    MOTOR_CURRENT: 0,
    MOTOR_TORQUE: 0,
    "profibus-cu-fault-id": 0,
    "profibus-rec-fault-id": 0,
    INV_FAULT_ID: 0,
}

#: Без этих сигналов модель не может показать ни рейс, ни аварию.
REQUIRED_SIGNALS = frozenset(
    {
        ESTOP_CONSOLE,
        DRIVE_EMERGENCY_OFF,
        KAR_ZERO,
        KAR_UP,
        KAR_DOWN,
        KRT_BRAKED,
        KRT_RELEASED,
        ACKNOWLEDGE,
        RESET_TP,
        TP_RELAY,
        MOTOR_SPEED,
        MOTOR_CURRENT,
        INV_STATUS,
        INV_STATUS_1,
        INV_FAULT_ID,
    }
)

#: Значения исправного состояния, которые из программы ПЛК однозначно не
#: следуют. Их надо подтвердить на объекте: неверная полярность даёт станции
#: «аварию» в исправном состоянии.
ASSUMPTIONS: Dict[str, str] = {
    "ch1-10a3-sq5": "полярность датчика переподъёма канала 1 выведена по косвенному признаку",
    "ch1-10a3-sq6": "полярность датчика переподъёма канала 1 выведена по косвенному признаку",
    "ch2-20a3-sq5": "в канале 2 вход датчика переподъёма инвертирован в программе",
    "ch2-20a3-sq6": "в канале 2 вход датчика переподъёма инвертирован в программе",
    "ch1-10a3-sq11": "полярность датчика провисания каната не подтверждена",
    "ch1-10a3-sq12": "полярность датчика провисания каната не подтверждена",
    "ch1-10a3-k2": "второе реле ТП: в работе принято 0",
    "ch2-20a3-k2": "второе реле ТП канала 2: принято как в канале 1",
    "shvp-k86": "в имени тега — неисправность вентилятора, в описании — обратная связь",
    "shvp-k87": "в имени тега — неисправность вентилятора, в описании — обратная связь",
    "shvp-k1": "по программе разрешение включения требует 0, смысл контакта не подтверждён",
    "shvp-k2": "контакты вводов участвуют только в отключённой части программы",
    "shvp-k3": "контакты вводов участвуют только в отключённой части программы",
    "shvp-k4": "контакты вводов участвуют только в отключённой части программы",
    "pu-sa13-left": "источник команд выбран «левая клеть»: нужен один из ключей клетей",
}

# ---------------------------------------------------------------------------
# Кинематика рейса (масштаб привода: 16384 = 100 % = 4,7 м/с)
# ---------------------------------------------------------------------------

SPEED_CRUISE = 12200  # ≈ 3,5 м/с
ACCEL_TICKS = 30  # разгон 3 с
DECEL_TICKS = 30  # штатное замедление 3 с
SAFETY_BRAKE_TICKS = 20  # наложение предохранительного тормоза 2 с
TRIP_CRUISE_TICKS = 80  # установившийся ход полного рейса 8 с: длина ствола постоянна
COUNTS_AT_CRUISE = 350  # импульсов энкодера за 100 мс на крейсерской скорости
FRICTION_ROLLER_RATIO = 97  # ролик канала 2 даёт 97 % импульсов вала, %
CURRENT_ACCEL, CURRENT_CRUISE, CURRENT_DECEL = 8800, 5200, 2600
TORQUE_ACCEL, TORQUE_CRUISE, TORQUE_DECEL = 9000, 4100, -2500
OVERCURRENT = 15800
DWELL_MS = 5000  # стоянка под загрузкой между рейсами
HOLD_MS = 20000  # машина стоит после аварии до действий персонала
INVERTER_FAULT_CODE = 7  # условный код: смысл кодов привода имитатор не моделирует


def _counts(speed: int) -> int:
    """Импульсов энкодера вала за один шаг 100 мс на данной скорости."""
    return round(abs(speed) * COUNTS_AT_CRUISE / SPEED_CRUISE)


ACCEL_COUNTS = sum(_counts(SPEED_CRUISE * step // ACCEL_TICKS) for step in range(1, ACCEL_TICKS + 1))
DECEL_COUNTS = sum(
    _counts(SPEED_CRUISE * (DECEL_TICKS - step) // DECEL_TICKS) for step in range(1, DECEL_TICKS + 1)
)
TRIP_COUNTS = ACCEL_COUNTS + TRIP_CRUISE_TICKS * COUNTS_AT_CRUISE + DECEL_COUNTS


@dataclass
class HoistState:
    """Состояние между циклами: где сосуды, что насчитали энкодеры, сколько пройдено в рейсе."""

    left_at_top: bool = True
    encoders: List[int] = field(default_factory=lambda: [0, 0, 0, 0])
    travelled: int = 0


def _motion(
    tl: Timeline,
    st: HoistState,
    t: int,
    speed: int,
    phase: str,
    rng: Optional[random.Random] = None,
    current_override: Optional[int] = None,
) -> None:
    """Один шаг 100 мс: скорость, ток, момент и импульсы энкодеров.

    На установившемся ходу ток и момент чуть пульсируют — живой привод, а не
    константа; в остальных фазах значения детерминированы без случайности.
    """
    tl.set(t, MOTOR_SPEED, speed)
    ripple = rng.randint(-40, 40) if phase == "cruise" and rng is not None else 0
    current, torque = {
        "accel": (CURRENT_ACCEL, TORQUE_ACCEL),
        "cruise": (CURRENT_CRUISE + ripple, TORQUE_CRUISE + ripple),
        "decel": (CURRENT_DECEL, TORQUE_DECEL),
        "coast": (0, 0),
    }[phase]
    if speed == 0 and phase != "accel":
        current, torque = 0, 0
    if current_override is not None:
        current = current_override
    tl.set(t, MOTOR_CURRENT, current)
    tl.set(t, MOTOR_TORQUE, torque)
    counts = _counts(speed)
    st.travelled += counts
    sign = 1 if speed >= 0 else -1
    per_encoder = (counts, counts, counts * FRICTION_ROLLER_RATIO // 100, counts * FRICTION_ROLLER_RATIO // 100)
    for index, signal_id in enumerate(ENCODERS):
        st.encoders[index] += sign * per_encoder[index]
        tl.set(t, signal_id, wrap_int16(st.encoders[index]))


def _direction(st: HoistState) -> int:
    """+1 — «Вверх» (поднимает левый сосуд); из положения «левый вверху» — вниз."""
    return -1 if st.left_at_top else 1


def _begin_trip(tl: Timeline, t: int, direction: int) -> int:
    """Растормозить и дать команду рукояткой КАР; вернуть момент начала разгона."""
    tl.set(t, KRT_BRAKED, False)
    tl.set(t, KRT_RELEASED, True)
    tl.set_bits(t, INV_STATUS_1, SW1_RELEASE_HOLD_BRAKE, True)
    t += 500
    tl.set(t, KAR_ZERO, False)
    tl.set(t, KAR_UP if direction > 0 else KAR_DOWN, True)
    tl.set_bits(t, INV_STATUS, INV_FUNCTION | INV_NORMAL_OPER, True)
    tl.set_bits(t, INV_STATUS_1, SW1_XFUNCTION, True)
    tl.set_bits(t, INV_STATUS_1, SW1_ROTATION_DIR, direction < 0)
    tl.set_bits(t, INV_STATUS_2, SW2_RAMP_ACTIVE, True)
    return t


def _accelerate(tl: Timeline, st: HoistState, t: int, direction: int, rng: random.Random) -> int:
    departing = SYNC_LEFT if st.left_at_top else SYNC_RIGHT
    for step in range(1, ACCEL_TICKS + 1):
        t += TICK_MS
        _motion(tl, st, t, direction * SPEED_CRUISE * step // ACCEL_TICKS, "accel", rng)
        if step == 5:
            for signal_id in departing:
                tl.set(t, signal_id, False)
    tl.set_bits(t, INV_STATUS_2, SW2_RAMP_ACTIVE, False)
    tl.set_bits(t, INV_STATUS_1, SW1_SPEED_REACHED, True)
    return t


def _cruise(tl: Timeline, st: HoistState, t: int, direction: int, ticks: int, rng: random.Random) -> int:
    for _ in range(ticks):
        t += TICK_MS
        _motion(tl, st, t, direction * SPEED_CRUISE, "cruise", rng)
    return t


def _decelerate(tl: Timeline, st: HoistState, t: int, direction: int, rng: random.Random) -> int:
    start = abs(int(tl.value(MOTOR_SPEED) or 0))
    tl.set_bits(t, INV_STATUS_2, SW2_RAMP_ACTIVE, True)
    tl.set_bits(t, INV_STATUS_1, SW1_SPEED_REACHED, False)
    for step in range(1, DECEL_TICKS + 1):
        t += TICK_MS
        _motion(tl, st, t, direction * start * (DECEL_TICKS - step) // DECEL_TICKS, "decel", rng)
    tl.set_bits(t, INV_STATUS_2, SW2_RAMP_ACTIVE, False)
    return t


def _park(tl: Timeline, t: int, direction: int) -> int:
    """Машинист возвращает КАР в нуль и затормаживает; привод снимает работу."""
    t += 300
    tl.set(t, KAR_UP if direction > 0 else KAR_DOWN, False)
    tl.set(t, KAR_ZERO, True)
    tl.set_bits(t, INV_STATUS, INV_FUNCTION | INV_NORMAL_OPER, False)
    tl.set_bits(t, INV_STATUS_1, SW1_XFUNCTION | SW1_ROTATION_DIR, False)
    t += 500
    tl.set(t, KRT_BRAKED, True)
    tl.set(t, KRT_RELEASED, False)
    tl.set_bits(t, INV_STATUS_1, SW1_RELEASE_HOLD_BRAKE, False)
    return t


def _arrive(tl: Timeline, st: HoistState, t: int) -> None:
    arriving = SYNC_RIGHT if st.left_at_top else SYNC_LEFT
    for signal_id in arriving:
        tl.set(t, signal_id, True)
    st.left_at_top = not st.left_at_top
    st.travelled = 0


def _remaining_cruise_ticks(st: HoistState) -> int:
    """Сколько идти на крейсерской скорости, чтобы дотянуть рейс до конца ствола."""
    remaining = TRIP_COUNTS - st.travelled - ACCEL_COUNTS - DECEL_COUNTS
    return max(0, round(remaining / COUNTS_AT_CRUISE))


def _full_trip(tl: Timeline, st: HoistState, t: int, rng: random.Random) -> int:
    direction = _direction(st)
    t = _begin_trip(tl, t, direction)
    t = _accelerate(tl, st, t, direction, rng)
    t = _cruise(tl, st, t, direction, TRIP_CRUISE_TICKS, rng)
    t = _decelerate(tl, st, t, direction, rng)
    _arrive(tl, st, t)
    t = _park(tl, t, direction)
    return t + DWELL_MS


# ---------------------------------------------------------------------------
# Эпизоды
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CauseChange:
    signal_id: str
    before: Any
    after: Any


@dataclass(frozen=True)
class GroundTruth:
    """Что на самом деле произошло — эталон для оценки ответа станции."""

    kind: str
    title: str
    fault: bool
    causes: Tuple[CauseChange, ...]
    cause_at_ms: int
    stopped_at_ms: int
    recovered_at_ms: int
    expected_answer: str
    cause_visible_after_stop: bool


@dataclass(frozen=True)
class Cycle:
    changes: Tuple[Change, ...]
    duration_ms: int
    truth: GroundTruth


@dataclass(frozen=True)
class EpisodeKind:
    kind: str
    title: str
    fault: bool
    expected_answer: str


KINDS: Dict[str, EpisodeKind] = {
    item.kind: item
    for item in (
        EpisodeKind(
            "estop_console",
            "Аварийный стоп с пульта ПМ",
            True,
            "На ходу разомкнулся аварийный стоп пульта ПМ; защита сняла разрешение "
            "работы привода, отпало реле ТП, машина остановлена предохранительным "
            "тормозом. Стоп оставался нажатым до осмотра.",
        ),
        EpisodeKind(
            "estop_circuit_glitch",
            "Кратковременный разрыв цепи аварийного стопа",
            True,
            "На ходу цепь аварийного стопа пульта ПМ разомкнулась на 300 мс и "
            "восстановилась; защита ТП успела сработать и зафиксировалась, машина "
            "остановлена предохранительным тормозом. После остановки стоп в норме — "
            "причина видна только в истории.",
        ),
        EpisodeKind(
            "drive_emergency_off",
            "Аварийное отключение ПЧ",
            True,
            "На ходу нажата кнопка аварийного отключения ПЧ; защита сняла разрешение "
            "работы привода, отпало реле ТП, машина остановлена предохранительным "
            "тормозом.",
        ),
        EpisodeKind(
            "inverter_fault",
            "Авария инвертора",
            True,
            "На ходу ток двигателя вырос до перегрузки, инвертор выставил бит аварии "
            "в слове состояния и код ошибки; защита ТП сработала, отпало реле ТП, "
            "машина остановлена предохранительным тормозом.",
        ),
        EpisodeKind(
            "tp_relay_dropout",
            "Отпадание реле ТП на ходу",
            True,
            "На ходу отпало реле ТП, хотя ни один защитный вход не менялся перед "
            "этим; привод остановлен предохранительным тормозом. Причина — в цепи "
            "самого реле ТП, а не в срабатывании защиты.",
        ),
        EpisodeKind(
            "normal_stop",
            "Штатная остановка машинистом",
            False,
            "Аварии не было: машинист вернул рукоятку КАР в нуль посреди ствола, "
            "привод штатно замедлил машину, защиты не срабатывали.",
        ),
    )
}


def _safety_stop(tl: Timeline, st: HoistState, t_fault: int, direction: int, *, relay_is_cause: bool) -> int:
    """Срабатывание ТП: реле отпадает, привод снят, ТП тормозит; вернуть момент остановки."""
    t = t_fault + TICK_MS
    if not relay_is_cause:
        tl.set(t, TP_RELAY, False)
    tl.set_bits(t, INV_STATUS, INV_FUNCTION | INV_NORMAL_OPER, False)
    tl.set_bits(t, INV_STATUS_1, SW1_XFUNCTION | SW1_SPEED_REACHED | SW1_RELEASE_HOLD_BRAKE, False)
    tl.set_bits(t, INV_STATUS_2, SW2_RAMP_ACTIVE, False)
    # Машинист реагирует на остановку: КАР в нуль, КРТ в «заторможено».
    tl.set(t_fault + 900, KAR_UP if direction > 0 else KAR_DOWN, False)
    tl.set(t_fault + 900, KAR_ZERO, True)
    tl.set(t_fault + 1500, KRT_BRAKED, True)
    tl.set(t_fault + 1500, KRT_RELEASED, False)
    start = abs(int(tl.value(MOTOR_SPEED) or 0))
    t -= TICK_MS
    for step in range(1, SAFETY_BRAKE_TICKS + 1):
        t += TICK_MS
        _motion(tl, st, t, direction * start * (SAFETY_BRAKE_TICKS - step) // SAFETY_BRAKE_TICKS, "coast")
    tl.set_bits(t, INV_STATUS_1, SW1_ROTATION_DIR, False)
    return t


def _acknowledge_and_reset(tl: Timeline, t: int) -> int:
    """Персонал квитирует аварию и заряжает ТП; реле ТП снова под током."""
    tl.set(t, ACKNOWLEDGE, True)
    tl.set(t + 300, ACKNOWLEDGE, False)
    tl.set_bits(t + 400, INV_STATUS, INV_FAULT, False)
    tl.set_bits(t + 400, INV_STATUS_1, SW1_FAULT | SW1_ALARM, False)
    tl.set(t + 400, INV_FAULT_ID, 0)
    t += 1000
    tl.set(t, RESET_TP, True)
    tl.set(t + 200, TP_RELAY, True)
    tl.set(t + 500, RESET_TP, False)
    return t + 2000


def build_cycle(kind: str, rng: random.Random, st: HoistState, current: Dict[str, Any], known: Iterable[str]) -> Tuple[Cycle, Dict[str, Any]]:
    """Один цикл: 1–2 штатных рейса, затем рейс с эпизодом и возврат в работу.

    Возвращает цикл и значения сигналов на его конце — начало следующего цикла.
    """
    episode = KINDS[kind]
    tl = Timeline(current, known)
    t = 0
    for _ in range(rng.randint(1, 2)):
        t = _full_trip(tl, st, t, rng)

    direction = _direction(st)
    t = _begin_trip(tl, t, direction)
    t = _accelerate(tl, st, t, direction, rng)
    cruise_before = rng.randint(20, 50)
    precursor = 6 if kind == "inverter_fault" else 0
    t = _cruise(tl, st, t, direction, cruise_before - precursor, rng)
    for step in range(1, precursor + 1):
        t += TICK_MS
        rising = CURRENT_CRUISE + (OVERCURRENT - CURRENT_CRUISE) * step // precursor
        _motion(tl, st, t, direction * SPEED_CRUISE, "cruise", rng, current_override=rising)
    t_cause = t + TICK_MS
    causes: List[CauseChange] = []
    restore: Optional[Callable[[int], None]] = None

    if kind == "normal_stop":
        tl.set(t_cause, KAR_UP if direction > 0 else KAR_DOWN, False)
        tl.set(t_cause, KAR_ZERO, True)
        causes.append(CauseChange(KAR_ZERO, False, True))
        t = _decelerate(tl, st, t_cause, direction, rng)
        stopped = t
        tl.set_bits(t + 300, INV_STATUS, INV_FUNCTION | INV_NORMAL_OPER, False)
        tl.set_bits(t + 300, INV_STATUS_1, SW1_XFUNCTION | SW1_ROTATION_DIR, False)
        tl.set(t + 800, KRT_BRAKED, True)
        tl.set(t + 800, KRT_RELEASED, False)
        tl.set_bits(t + 800, INV_STATUS_1, SW1_RELEASE_HOLD_BRAKE, False)
        recovered = t + 800 + 10000
    else:
        relay_is_cause = False
        if kind == "estop_console":
            tl.set(t_cause, ESTOP_CONSOLE, False)
            causes.append(CauseChange(ESTOP_CONSOLE, True, False))
            restore = lambda at: tl.set(at, ESTOP_CONSOLE, True)  # noqa: E731
        elif kind == "estop_circuit_glitch":
            tl.set(t_cause, ESTOP_CONSOLE, False)
            tl.set(t_cause + 300, ESTOP_CONSOLE, True)
            causes.append(CauseChange(ESTOP_CONSOLE, True, False))
        elif kind == "drive_emergency_off":
            tl.set(t_cause, DRIVE_EMERGENCY_OFF, False)
            causes.append(CauseChange(DRIVE_EMERGENCY_OFF, True, False))
            restore = lambda at: tl.set(at, DRIVE_EMERGENCY_OFF, True)  # noqa: E731
        elif kind == "inverter_fault":
            before = tl.value(INV_STATUS)
            tl.set_bits(t_cause, INV_STATUS, INV_FAULT, True)
            tl.set_bits(t_cause, INV_STATUS_1, SW1_FAULT | SW1_ALARM, True)
            tl.set(t_cause, INV_FAULT_ID, INVERTER_FAULT_CODE)
            causes.append(CauseChange(INV_STATUS, before, tl.value(INV_STATUS)))
            causes.append(CauseChange(INV_FAULT_ID, 0, INVERTER_FAULT_CODE))
        elif kind == "tp_relay_dropout":
            tl.set(t_cause, TP_RELAY, False)
            causes.append(CauseChange(TP_RELAY, True, False))
            relay_is_cause = True
        else:  # pragma: no cover — KINDS и ветки выше перечисляют одно и то же
            raise ValueError(kind)
        stopped = _safety_stop(tl, st, t_cause, direction, relay_is_cause=relay_is_cause)
        t = stopped + HOLD_MS
        if restore is not None:
            restore(t)
        recovered = _acknowledge_and_reset(tl, t + 2000)

    # Машина дотягивает рейс до конечного положения и встаёт под загрузку.
    t = _begin_trip(tl, recovered, direction)
    t = _accelerate(tl, st, t, direction, rng)
    t = _cruise(tl, st, t, direction, _remaining_cruise_ticks(st), rng)
    t = _decelerate(tl, st, t, direction, rng)
    _arrive(tl, st, t)
    t = _park(tl, t, direction) + DWELL_MS

    truth = GroundTruth(
        kind=kind,
        title=episode.title,
        fault=episode.fault,
        causes=tuple(causes),
        cause_at_ms=t_cause,
        stopped_at_ms=stopped,
        recovered_at_ms=recovered,
        expected_answer=episode.expected_answer,
        cause_visible_after_stop=kind not in ("estop_circuit_glitch",),
    )
    return Cycle(changes=tl.changes(), duration_ms=t, truth=truth), tl.state()
