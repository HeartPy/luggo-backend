"""
日次の集荷・配達を配達者へ割り当てる（訪問順序は計算しない）

入力は task_builder が用意した配達者と集荷・配達の一覧。
同じ予約の集荷と配達はセットで扱い、OR-Tools（CP-SAT）で誰が持つかを決める。
距離は道路ではなく地図上の直線距離の目安。偏りや未割当はペナルティで抑える。
"""
import math
from dataclasses import dataclass

from django.conf import settings
from ortools.sat.python import cp_model

from .task_builder import DriverInput, TaskInput


@dataclass(frozen=True)
class AssignedTask:
    task: TaskInput
    driver: DriverInput


@dataclass
class AssignmentResult:
    assignments: list[AssignedTask]
    unassigned: list[dict]


@dataclass(frozen=True)
class _TaskGroup:
    """同じ予約の集荷・配達などを1まとまりにした単位（割当の最小単位）"""
    id: str
    tasks: tuple[TaskInput, ...]
    fixed_driver_id: str | None

    @property
    def stop_count(self) -> int:
        return len(self.tasks)

    @property
    def luggage_count(self) -> int:
        # 同日ペアでも荷物は1セットなので、1件分だけ返す
        if not self.tasks:
            return 0
        return self.tasks[0].luggage_count


def _haversine_meters(
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> int:
    """2点間の直線距離（メートル）"""
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    # 地球半径 6371km を使った大円距離
    return round(2 * 6_371_000 * math.asin(math.sqrt(value)))


def _group_tasks(tasks: list[TaskInput]) -> list[_TaskGroup]:
    """集荷・配達を pair_id 単位のグループにまとめる"""
    grouped: dict[str, list[TaskInput]] = {}
    for task in sorted(tasks, key=lambda task: task.id):
        grouped.setdefault(task.pair_id or task.id, []).append(task)
    result = []
    for group_id, members in sorted(grouped.items()):
        # 手動固定があればその配達者 ID（グループ内で一致している想定）
        fixed_ids = {
            task.fixed_driver_id for task in members if task.fixed_driver_id
        }
        fixed_driver_id = next(iter(fixed_ids), None)
        result.append(_TaskGroup(group_id, tuple(members), fixed_driver_id))
    return result


def _group_cost(driver: DriverInput, group: _TaskGroup) -> int:
    """配達者とそのグループの距離コスト（小さいほど近い）"""
    depot = (driver.latitude, driver.longitude)
    pickup = next((task for task in group.tasks if task.kind == 'pickup'), None)
    delivery = next((task for task in group.tasks if task.kind == 'delivery'), None)
    if pickup and delivery:
        # 出発地点 → 集荷 → 配達 の直線距離合計（訪問順そのものは決めない）
        pickup_point = (pickup.latitude, pickup.longitude)
        return (
            _haversine_meters(depot, pickup_point)
            + _haversine_meters(
                pickup_point,
                (delivery.latitude, delivery.longitude),
            )
        )
    task = pickup or delivery
    return _haversine_meters(depot, (task.latitude, task.longitude))


def _unassigned_items(group: _TaskGroup, reason: str) -> list[dict]:
    return [{
        'task_id': task.id,
        'booking_id': task.booking_id,
        'booking_number': task.booking_number,
        'task_type': task.kind,
        'reason': reason,
    } for task in group.tasks]


def assign_tasks(
    drivers: list[DriverInput],
    tasks: list[TaskInput],
) -> AssignmentResult:
    """グループ単位で配達者へ割り当てる。回る順番は計算しない。"""
    groups = _group_tasks(tasks)
    if not groups:
        return AssignmentResult([], [])
    if not drivers:
        return AssignmentResult(
            [],
            [
                item
                for group in groups
                for item in _unassigned_items(group, 'no_eligible_drivers')
            ],
        )

    # 配達者順を固定し、解の再現性を高める
    ordered_drivers = sorted(drivers, key=lambda driver: driver.id)
    driver_index = {
        driver.id: index for index, driver in enumerate(ordered_drivers)
    }
    model = cp_model.CpModel()
    # assigned[グループ, 配達者] / unassigned[グループ] は
    # はい(1)／いいえ(0) の選択変数
    assigned = {}
    unassigned = {}
    costs: dict[tuple[int, int], int] = {}

    for group_index, group in enumerate(groups):
        unassigned[group_index] = model.new_bool_var(f'unassigned_{group_index}')
        choices = []
        for index, driver in enumerate(ordered_drivers):
            variable = model.new_bool_var(f'assigned_{group_index}_{index}')
            assigned[group_index, index] = variable
            costs[group_index, index] = _group_cost(driver, group)
            choices.append(variable)
            # 手動固定があるグループは、指定以外の配達者に付けられない
            if group.fixed_driver_id and group.fixed_driver_id != driver.id:
                model.add(variable == 0)
        if group.fixed_driver_id in driver_index:
            # 固定先が候補にいるなら未割当は禁止
            model.add(unassigned[group_index] == 0)
        # ちょうど1人に付くか、未割当か（排他）
        model.add(sum(choices) + unassigned[group_index] == 1)

    # 各配達者の訪問数（ストップ数）が max_stops を超えない（設定がない配達者は無制限）
    total_stop_demand = sum(group.stop_count for group in groups)
    load_variables = []
    for index, driver in enumerate(ordered_drivers):
        stop_cap = (
            driver.max_stops
            if driver.max_stops is not None
            else total_stop_demand
        )
        load = model.new_int_var(0, stop_cap, f'load_{index}')
        model.add(
            load == sum(
                group.stop_count * assigned[group_index, index]
                for group_index, group in enumerate(groups)
            )
        )
        load_variables.append(load)

    # 各配達者の荷物個数が max_luggage_count を超えない（設定がない配達者は無制限）
    total_luggage_demand = sum(group.luggage_count for group in groups)
    for index, driver in enumerate(ordered_drivers):
        luggage_cap = (
            driver.max_luggage_count
            if driver.max_luggage_count is not None
            else total_luggage_demand
        )
        luggage_load = model.new_int_var(0, luggage_cap, f'luggage_load_{index}')
        model.add(
            luggage_load == sum(
                group.luggage_count * assigned[group_index, index]
                for group_index, group in enumerate(groups)
            )
        )

    # 配達者間の負荷差 (max_load - min_load) を後でペナルティに使う
    max_capacity = max(
        (
            driver.max_stops
            if driver.max_stops is not None
            else total_stop_demand
        )
        for driver in ordered_drivers
    )
    max_load = model.new_int_var(0, max_capacity, 'max_load')
    min_load = model.new_int_var(0, max_capacity, 'min_load')
    model.add_max_equality(max_load, load_variables)
    model.add_min_equality(min_load, load_variables)

    distance_weight = max(
        0, int(getattr(settings, 'ROUTING_ASSIGNMENT_DISTANCE_WEIGHT', 1))
    )
    balance_penalty = max(
        0, int(getattr(settings, 'ROUTING_ASSIGNMENT_BALANCE_PENALTY', 10_000))
    )
    dropped_penalty = max(
        1,
        int(
            getattr(
                settings,
                'ROUTING_ASSIGNMENT_UNASSIGNED_PENALTY',
                1_000_000_000,
            )
        ),
    )
    # 最小化: 距離コスト + 負荷の偏り + 未割当（未割当ペナルティが最も大きい）
    model.minimize(
        distance_weight
        * sum(
            costs[group_index, index] * variable
            for (group_index, index), variable in assigned.items()
        )
        + balance_penalty * (max_load - min_load)
        + dropped_penalty * sum(unassigned.values())
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(
        1,
        int(getattr(settings, 'ROUTING_ASSIGNMENT_TIME_LIMIT_SECONDS', 60)),
    )
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return AssignmentResult(
            [],
            [
                item
                for group in groups
                for item in _unassigned_items(group, 'assignment_infeasible')
            ],
        )

    # ソルバーの はい(1)／いいえ(0) の解を AssignedTask / 未割当リストに戻す
    results = []
    unassigned_results = []
    for group_index, group in enumerate(groups):
        if solver.value(unassigned[group_index]):
            unassigned_results.extend(
                _unassigned_items(group, 'assignment_capacity_exceeded')
            )
            continue
        for index, driver in enumerate(ordered_drivers):
            if solver.value(assigned[group_index, index]):
                results.extend(
                    AssignedTask(task=task, driver=driver)
                    for task in group.tasks
                )
                break

    return AssignmentResult(
        assignments=sorted(results, key=lambda item: item.task.id),
        unassigned=unassigned_results,
    )
