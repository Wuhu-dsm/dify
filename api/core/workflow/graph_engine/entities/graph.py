import uuid
from typing import Optional, cast

from pydantic import BaseModel, Field

from core.workflow.entities.node_entities import NodeRunResult, NodeType
from core.workflow.entities.variable_pool import VariablePool
from core.workflow.graph_engine.condition_handlers.condition_manager import ConditionManager
from core.workflow.graph_engine.entities.run_condition import RunCondition


class GraphEdge(BaseModel):
    source_node_id: str
    """source node id"""

    target_node_id: str
    """target node id"""

    run_condition: Optional[RunCondition] = None
    """condition to run the edge"""


class GraphParallel(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    """random uuid parallel id"""

    parent_parallel_id: Optional[str] = None
    """parent parallel id if exists"""


class GraphStateRoute(BaseModel):
    route_id: str
    """route id"""

    node_id: str
    """node id"""


class GraphState(BaseModel):
    routes: dict[str, list[GraphStateRoute]] = Field(default_factory=dict)
    """graph state routes (source_node_id: routes)"""

    variable_pool: VariablePool
    """variable pool"""

    node_route_results: dict[str, NodeRunResult] = Field(default_factory=dict)
    """node results in route (node_id: run_result)"""


class NextGraphNode(BaseModel):
    node_id: str
    """next node id"""

    parallel: Optional[GraphParallel] = None
    """parallel"""


class Graph(BaseModel):
    root_node_id: str
    """root node id of the graph"""

    node_ids: list[str] = Field(default_factory=list)
    """graph node ids"""

    edge_mapping: dict[str, list[GraphEdge]] = Field(default_factory=dict)
    """graph edge mapping (source node id: edges)"""

    parallel_mapping: dict[str, GraphParallel] = Field(default_factory=dict)
    """graph parallel mapping (parallel id: parallel)"""

    node_parallel_mapping: dict[str, str] = Field(default_factory=dict)
    """graph node parallel mapping (node id: parallel id)"""

    run_state: GraphState
    """graph run state"""

    @classmethod
    def init(cls,
             graph_config: dict,
             variable_pool: VariablePool,
             root_node_id: Optional[str] = None) -> "Graph":
        """
        Init graph

        :param graph_config: graph config
        :param variable_pool: variable pool
        :param root_node_id: root node id
        :return: graph
        """
        # edge configs
        edge_configs = graph_config.get('edges')
        if edge_configs is None:
            edge_configs = []

        edge_configs = cast(list, edge_configs)

        # reorganize edges mapping
        edge_mapping: dict[str, list[GraphEdge]] = {}
        target_edge_ids = set()
        for edge_config in edge_configs:
            source_node_id = edge_config.get('source')
            if not source_node_id:
                continue

            if source_node_id not in edge_mapping:
                edge_mapping[source_node_id] = []

            target_node_id = edge_config.get('target')
            if not target_node_id:
                continue

            target_edge_ids.add(target_node_id)

            # parse run condition
            run_condition = None
            if edge_config.get('sourceHandle'):
                run_condition = RunCondition(
                    type='branch_identify',
                    branch_identify=edge_config.get('sourceHandle')
                )

            graph_edge = GraphEdge(
                source_node_id=source_node_id,
                target_node_id=edge_config.get('target'),
                run_condition=run_condition
            )

            edge_mapping[source_node_id].append(graph_edge)

        # node configs
        node_configs = graph_config.get('nodes')
        if not node_configs:
            raise ValueError("Graph must have at least one node")

        node_configs = cast(list, node_configs)

        # fetch nodes that have no predecessor node
        root_node_configs = []
        for node_config in node_configs:
            node_id = node_config.get('id')
            if not node_id:
                continue

            if node_id not in target_edge_ids:
                root_node_configs.append(node_config)

        root_node_ids = [node_config.get('id') for node_config in root_node_configs]

        # fetch root node
        if not root_node_id:
            # if no root node id, use the START type node as root node
            root_node_id = next((node_config for node_config in root_node_configs
                                 if node_config.get('data', {}).get('type', '') == NodeType.START.value), None)

        if not root_node_id or root_node_id not in root_node_ids:
            raise ValueError(f"Root node id {root_node_id} not found in the graph")

        # fetch all node ids from root node
        node_ids = [root_node_id]
        cls._recursively_add_node_ids(
            node_ids=node_ids,
            edge_mapping=edge_mapping,
            node_id=root_node_id
        )

        # init parallel mapping
        parallel_mapping: dict[str, GraphParallel] = {}
        node_parallel_mapping: dict[str, str] = {}
        cls._recursively_add_parallels(
            edge_mapping=edge_mapping,
            start_node_id=root_node_id,
            parallel_mapping=parallel_mapping,
            node_parallel_mapping=node_parallel_mapping
        )

        # init graph
        graph = cls(
            root_node_id=root_node_id,
            node_ids=node_ids,
            edge_mapping=edge_mapping,
            run_state=GraphState(
                variable_pool=variable_pool
            ),
            parallel_mapping=parallel_mapping,
            node_parallel_mapping=node_parallel_mapping
        )

        return graph

    @classmethod
    def _recursively_add_node_ids(cls,
                                  node_ids: list[str],
                                  edge_mapping: dict[str, list[GraphEdge]],
                                  node_id: str) -> None:
        """
        Recursively add node ids

        :param node_ids: node ids
        :param edge_mapping: edge mapping
        :param node_id: node id
        """
        for graph_edge in edge_mapping.get(node_id, []):
            if graph_edge.target_node_id in node_ids:
                continue

            node_ids.append(graph_edge.target_node_id)
            cls._recursively_add_node_ids(
                node_ids=node_ids,
                edge_mapping=edge_mapping,
                node_id=graph_edge.target_node_id
            )

    def next_node_ids(self) -> list[NextGraphNode]:
        """
        Get next node ids
        """
        # get current node ids in state
        if not self.run_state.routes:
            return [NextGraphNode(node_id=self.root_node_id)]

        route_final_graph_edges: list[GraphEdge] = []
        for route in self.run_state.routes[self.root_node_id]:
            graph_edges = self.edge_mapping.get(route.node_id)
            if not graph_edges:
                continue

            for edge in graph_edges:
                if edge.target_node_id not in self.run_state.routes:
                    route_final_graph_edges.append(edge)

        next_graph_nodes = []
        for route_final_graph_edge in route_final_graph_edges:
            node_id = route_final_graph_edge.target_node_id
            # check condition
            if route_final_graph_edge.run_condition:
                result = ConditionManager.get_condition_handler(
                    run_condition=route_final_graph_edge.run_condition
                ).check(
                    source_node_id=route_final_graph_edge.source_node_id,
                    target_node_id=route_final_graph_edge.target_node_id,
                    graph=self
                )

                if not result:
                    continue

            parallel = None
            if route_final_graph_edge.target_node_id in self.node_parallel_mapping:
                parallel = self.parallel_mapping[self.node_parallel_mapping[node_id]]

            next_graph_nodes.append(NextGraphNode(node_id=node_id, parallel=parallel))

        return next_graph_nodes

    def add_extra_edge(self, source_node_id: str,
                       target_node_id: str,
                       run_condition: Optional[RunCondition] = None) -> None:
        """
        Add extra edge to the graph

        :param source_node_id: source node id
        :param target_node_id: target node id
        :param run_condition: run condition
        """
        if source_node_id not in self.node_ids or target_node_id not in self.node_ids:
            return

        if source_node_id not in self.edge_mapping:
            self.edge_mapping[source_node_id] = []

        if target_node_id in [graph_edge.target_node_id for graph_edge in self.edge_mapping[source_node_id]]:
            return

        graph_edge = GraphEdge(
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            run_condition=run_condition
        )

        self.edge_mapping[source_node_id].append(graph_edge)

    def get_leaf_node_ids(self) -> list[str]:
        """
        Get leaf node ids of the graph

        :return: leaf node ids
        """
        leaf_node_ids = []
        for node_id in self.node_ids:
            if node_id not in self.edge_mapping:
                leaf_node_ids.append(node_id)
            elif (len(self.edge_mapping[node_id]) == 1
                  and self.edge_mapping[node_id][0].target_node_id == self.root_node_id):
                leaf_node_ids.append(node_id)

        return leaf_node_ids

    @classmethod
    def _recursively_add_parallels(cls,
                                   edge_mapping: dict[str, list[GraphEdge]],
                                   start_node_id: str,
                                   parallel_mapping: dict[str, GraphParallel],
                                   node_parallel_mapping: dict[str, str]) -> None:
        """
        Recursively add parallel ids

        :param edge_mapping: edge mapping
        :param start_node_id: start from node id
        :param parallel_mapping: parallel mapping
        :param node_parallel_mapping: node parallel mapping
        """
        target_node_edges = edge_mapping.get(start_node_id, [])
        if len(target_node_edges) > 1:
            # fetch all node ids in current parallels
            parallel_node_ids = [graph_edge.target_node_id
                                 for graph_edge in target_node_edges if graph_edge.run_condition is not None]

            # any target node id in node_parallel_mapping
            if parallel_node_ids:
                # all parallel_node_ids in node_parallel_mapping
                parent_parallel_id = None
                if all(node_id in node_parallel_mapping for node_id in parallel_node_ids):
                    parent_parallel_id = node_parallel_mapping[parallel_node_ids[0]]

                parallel = GraphParallel(parent_parallel_id=parent_parallel_id)
                parallel_mapping[parallel.id] = parallel

                in_branch_node_ids = cls._fetch_all_node_ids_in_parallels(
                    edge_mapping=edge_mapping,
                    parallel_node_ids=parallel_node_ids
                )

                node_parallel_mapping.update({node_id: parallel.id for node_id in in_branch_node_ids})

        for graph_edge in target_node_edges:
            cls._recursively_add_parallels(
                edge_mapping=edge_mapping,
                start_node_id=graph_edge.target_node_id,
                parallel_mapping=parallel_mapping,
                node_parallel_mapping=node_parallel_mapping
            )

    @classmethod
    def _recursively_add_parallel_node_ids(cls,
                                           branch_node_ids: list[str],
                                           edge_mapping: dict[str, list[GraphEdge]],
                                           merge_node_id: str,
                                           start_node_id: str) -> None:
        """
        Recursively add node ids

        :param branch_node_ids: in branch node ids
        :param edge_mapping: edge mapping
        :param merge_node_id: merge node id
        :param start_node_id: start node id
        """
        for graph_edge in edge_mapping.get(start_node_id, []):
            if (graph_edge.target_node_id != merge_node_id
                    and graph_edge.target_node_id not in branch_node_ids):
                branch_node_ids.append(graph_edge.target_node_id)
                cls._recursively_add_parallel_node_ids(
                    branch_node_ids=branch_node_ids,
                    edge_mapping=edge_mapping,
                    merge_node_id=merge_node_id,
                    start_node_id=graph_edge.target_node_id
                )

    @classmethod
    def _fetch_all_node_ids_in_parallels(cls,
                                         edge_mapping: dict[str, list[GraphEdge]],
                                         parallel_node_ids: list[str]) -> dict[str, list[str]]:
        """
        Fetch all node ids in parallels
        """
        routes_node_ids: dict[str, list[str]] = {}
        for parallel_node_id in parallel_node_ids:
            routes_node_ids[parallel_node_id] = []

            # fetch routes node ids
            cls._recursively_fetch_routes(
                edge_mapping=edge_mapping,
                start_node_id=parallel_node_id,
                routes_node_ids=routes_node_ids[parallel_node_id]
            )

        # fetch leaf node ids from routes node ids
        leaf_node_ids: dict[str, list[str]] = {}
        merge_branch_node_ids: dict[str, list[str]] = {}
        for branch_node_id, node_ids in routes_node_ids.items():
            for node_id in node_ids:
                if node_id not in edge_mapping or len(edge_mapping[node_id]) == 0:
                    if branch_node_id not in leaf_node_ids:
                        leaf_node_ids[branch_node_id] = []

                    leaf_node_ids[branch_node_id].append(node_id)

                for branch_node_id2, inner_route2 in routes_node_ids.items():
                    if branch_node_id != branch_node_id2 and node_id in inner_route2:
                        if node_id not in merge_branch_node_ids:
                            merge_branch_node_ids[node_id] = []

                        merge_branch_node_ids[node_id].append(branch_node_id2)

        # sorted merge_branch_node_ids by branch_node_ids length desc
        merge_branch_node_ids = dict(sorted(merge_branch_node_ids.items(), key=lambda x: len(x[1]), reverse=True))

        branches_merge_node_ids: dict[str, str] = {}
        for node_id, branch_node_ids in merge_branch_node_ids.items():
            if len(branch_node_ids) <= 1:
                continue

            for branch_node_id in branch_node_ids:
                if branch_node_id in branches_merge_node_ids:
                    continue

                branches_merge_node_ids[branch_node_id] = node_id

        in_branch_node_ids: dict[str, list[str]] = {}
        for branch_node_id, node_ids in routes_node_ids.items():
            in_branch_node_ids[branch_node_id] = [branch_node_id]
            if branch_node_id not in branches_merge_node_ids:
                # all node ids in current branch is in this thread
                in_branch_node_ids[branch_node_id].extend(node_ids)
            else:
                merge_node_id = branches_merge_node_ids[branch_node_id]
                # fetch all node ids from branch_node_id and merge_node_id
                cls._recursively_add_parallel_node_ids(
                    branch_node_ids=in_branch_node_ids[branch_node_id],
                    edge_mapping=edge_mapping,
                    merge_node_id=merge_node_id,
                    start_node_id=branch_node_id
                )

        return in_branch_node_ids

    @classmethod
    def _recursively_fetch_routes(cls,
                                  edge_mapping: dict[str, list[GraphEdge]],
                                  start_node_id: str,
                                  routes_node_ids: list[str]) -> None:
        """
        Recursively fetch route
        """
        if start_node_id not in edge_mapping:
            return

        for graph_edge in edge_mapping[start_node_id]:
            # find next node ids
            if graph_edge.target_node_id not in routes_node_ids:
                routes_node_ids.append(graph_edge.target_node_id)

                cls._recursively_fetch_routes(
                    edge_mapping=edge_mapping,
                    start_node_id=graph_edge.target_node_id,
                    routes_node_ids=routes_node_ids
                )