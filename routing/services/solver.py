"""
割当済みの集荷・配達の訪問順を配達者ごとに求めるソルバー

1人の配達者と、その配達者へ割当済みの集荷・配達を入力として受け取り、
出発地点から各訪問先を回り、出発地点へ戻る最短ルートを求める。
同日の集荷・配達は集荷を先にする。
"""
from dataclasses import dataclass

from django.conf import settings
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .task_builder import DriverInput, TaskInput


class SolverError(RuntimeError):
    pass


@dataclass
class SolvedStop:
    task: TaskInput
    duration_from_previous: int
    distance_from_previous: int


@dataclass
class SolvedRoute:
    driver: DriverInput
    stops: list[SolvedStop]
    total_duration: int
    total_distance: int


def solve_driver_route(
    driver: DriverInput,
    tasks: list[TaskInput],
    durations: list[list[int]],
    distances: list[list[int]],
) -> SolvedRoute:
    """割当済みの集荷・配達をすべて回り、出発地点へ戻るルートを求める"""
    if not tasks:
        return SolvedRoute(driver, [], 0, 0)

    # 行列は [出発地点] + [各集荷・配達] の正方行列
    matrix_size = len(tasks) + 1
    if (
        len(durations) != matrix_size
        or any(len(row) != matrix_size for row in durations)
    ):
        raise SolverError('所要時間行列のサイズが入力と一致しません')
    if (
        len(distances) != matrix_size
        or any(len(row) != matrix_size for row in distances)
    ):
        raise SolverError('距離行列のサイズが入力と一致しません')

    # 行列の0番を出発地点兼終点、1番以降を集荷・配達として扱う
    manager = pywrapcp.RoutingIndexManager(
        matrix_size,
        1,            # 車両（配達者）は常に1人
        [0],          # 出発: 出発地点ノード
        [0],          # 終了: 同じ出発地点ノード
    )
    routing = pywrapcp.RoutingModel(manager)
    service_seconds = int(getattr(settings, 'ROUTING_SERVICE_SECONDS_PER_STOP', 300))

    def duration_callback(from_index, to_index):
        """地点間の移動コスト（秒）を返す"""
        source = manager.IndexToNode(from_index)
        target = manager.IndexToNode(to_index)
        # 出発地点からの移動には作業時間を足さない（source == 0）
        return durations[source][target] + (
            service_seconds if source > 0 else 0
        )

    duration_callback_index = routing.RegisterTransitCallback(duration_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(duration_callback_index)
    # ルート上の「ここまでの所要時間」を計算し、集荷→配達の順番制約にだけ使う
    max_leg = max(max(row) for row in durations)
    time_horizon = max(1, (max_leg + service_seconds) * matrix_size)
    routing.AddDimension(
        duration_callback_index,
        0,
        time_horizon,
        True,
        'Time',
    )

    task_by_node = {
        index + 1: task for index, task in enumerate(tasks)
    }
    task_indices = {
        task.id: manager.NodeToIndex(node)
        for node, task in task_by_node.items()
    }

    # 同日ペアは集荷→配達の順にする（同一ルート内で必ず両方を回る）
    pairs: dict[str, dict[str, int]] = {}
    for task in tasks:
        if task.pair_id:
            pairs.setdefault(task.pair_id, {})[task.kind] = task_indices[task.id]
    solver = routing.solver()
    time_dimension = routing.GetDimensionOrDie('Time')
    for pair_id, pair in pairs.items():
        if 'pickup' not in pair or 'delivery' not in pair:
            continue
        pickup = pair['pickup']
        delivery = pair['delivery']
        routing.AddPickupAndDelivery(pickup, delivery)
        solver.Add(time_dimension.CumulVar(pickup) <= time_dimension.CumulVar(delivery))

    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    parameters.time_limit.seconds = int(
        getattr(settings, 'ROUTING_SOLVER_TIME_LIMIT_SECONDS', 10)
    )
    solution = routing.SolveWithParameters(parameters)
    if solution is None:
        raise SolverError('割当済みの集荷・配達をすべて回るルートを生成できません')

    # 解の経路を辿り、出発地点へ戻るまでの訪問順・合計を組み立てる
    index = routing.Start(0)
    stops = []
    total_duration = 0
    total_distance = 0
    while not routing.IsEnd(index):
        next_index = solution.Value(routing.NextVar(index))
        source = manager.IndexToNode(index)
        target = manager.IndexToNode(next_index)
        leg_duration = durations[source][target]
        leg_distance = distances[source][target]
        total_duration += leg_duration + (
            service_seconds if source in task_by_node else 0
        )
        total_distance += leg_distance
        # 出発地点へ戻る区間は合計に含めるが、訪問先には追加しない
        if target in task_by_node:
            task = task_by_node[target]
            stops.append(SolvedStop(task, leg_duration, leg_distance))
        index = next_index

    return SolvedRoute(driver, stops, total_duration, total_distance)
