"""图算法层(Phase 1·B) —— 借鉴 llm_wiki 的知识图谱引擎。

纯函数,不碰 DB。输入 nodes/edges(由 graph_insights.py 从 knowledge_edges 装载),
输出社区划分、中心节点、Adamic-Adar 链接预测、知识缺口。

依赖:networkx + python-louvain(import 名 `community`)。
所有函数对空图/极小图安全返回,不抛异常。
"""
from collections import Counter
from typing import Dict, List, Tuple


def build_graph(nodes: List[dict], edges: List[dict]):
    """从 nodes/edges 建无向带权图。

    nodes: [{"id","title","tags":[...]}, ...]
    edges: [{"source","target","weight","relation_type"}, ...]
    无向图用于社区检测与 Adamic-Adar(共同邻居类指标天然无向)。
    重复/反向边权重取最大。
    """
    import networkx as nx
    G = nx.Graph()
    for n in nodes:
        G.add_node(n["id"], title=n.get("title") or "Untitled", tags=n.get("tags") or [])
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if not s or not t or s == t or s not in G or t not in G:
            continue
        w = float(e.get("weight") or 0.5)
        if G.has_edge(s, t):
            if w > G[s][t].get("weight", 0):
                G[s][t]["weight"] = w
        else:
            G.add_edge(s, t, weight=w)
    return G


def _community_label(G, members: List[str]) -> str:
    """用社区内出现最多的 tag 作为社区标签;无 tag 则用成员数描述。"""
    tag_counter: Counter = Counter()
    for nid in members:
        for tag in G.nodes[nid].get("tags", []):
            tag_counter[tag] += 1
    if tag_counter:
        return tag_counter.most_common(1)[0][0]
    return f"{len(members)} 篇未归类"


def detect_communities(G, min_size: int = 2) -> List[dict]:
    """Louvain 社区检测。返回按规模降序的社区列表。

    每个社区:{id, label, size, members:[node_id], sample_titles:[...]}。
    孤立点(size==1)不计入社区,留给 knowledge_gaps 处理。
    """
    if G.number_of_nodes() < 3 or G.number_of_edges() < 1:
        return []
    import community as community_louvain  # python-louvain
    partition = community_louvain.best_partition(G, weight="weight", random_state=42)
    buckets: Dict[int, List[str]] = {}
    for node, cid in partition.items():
        buckets.setdefault(cid, []).append(node)

    out = []
    for cid, members in buckets.items():
        if len(members) < min_size:
            continue
        # 社区内按度排序取代表标题
        members_sorted = sorted(members, key=lambda n: G.degree(n), reverse=True)
        out.append({
            "id": int(cid),
            "label": _community_label(G, members),
            "size": len(members),
            "members": members_sorted,
            "sample_titles": [G.nodes[n]["title"] for n in members_sorted[:4]],
        })
    out.sort(key=lambda c: c["size"], reverse=True)
    # 重新编号为 0..k-1,便于前端上色
    for i, c in enumerate(out):
        c["id"] = i
    return out


def central_hubs(G, top_n: int = 8) -> List[dict]:
    """按加权度数取枢纽节点(知识库里串联最多的文章)。"""
    if G.number_of_nodes() == 0:
        return []
    deg = dict(G.degree(weight="weight"))
    raw_deg = dict(G.degree())
    ranked = sorted(deg.items(), key=lambda kv: kv[1], reverse=True)
    out = []
    for nid, wdeg in ranked[:top_n]:
        if raw_deg.get(nid, 0) == 0:
            continue
        out.append({
            "id": nid,
            "title": G.nodes[nid]["title"],
            "degree": raw_deg.get(nid, 0),
            "weighted_degree": round(float(wdeg), 3),
        })
    return out


def surprising_links(G, node_community: Dict[str, int], top_n: int = 8,
                     min_score: float = 1.0) -> List[dict]:
    """Adamic-Adar 链接预测,找「意外连接」——共享很多邻居却没有直接边的文章对。

    这类对很可能高度相关但还没被连上,值得提示用户对照阅读。
    跨社区的对额外标 cross_community(更"意外")。
    node_community: detect_communities 之外单独算的 node→cid 映射(可空)。
    """
    if G.number_of_nodes() < 4 or G.number_of_edges() < 2:
        return []
    import networkx as nx
    try:
        scored: List[Tuple[str, str, float]] = []
        for u, v, p in nx.adamic_adar_index(G):  # 默认遍历所有非邻接对
            if p >= min_score:
                scored.append((u, v, p))
    except Exception:
        return []
    scored.sort(key=lambda x: x[2], reverse=True)
    out = []
    for u, v, p in scored[:top_n]:
        cu = node_community.get(u)
        cv = node_community.get(v)
        out.append({
            "source": {"id": u, "title": G.nodes[u]["title"]},
            "target": {"id": v, "title": G.nodes[v]["title"]},
            "score": round(float(p), 3),
            "cross_community": (cu is not None and cv is not None and cu != cv),
        })
    return out


def knowledge_gaps(G, communities: List[dict]) -> dict:
    """知识缺口:孤立文章(没有任何连接的孤岛)+ 规模过小的主题簇。"""
    orphans = []
    for nid in G.nodes:
        if G.degree(nid) == 0:
            orphans.append({"id": nid, "title": G.nodes[nid]["title"]})
    small_topics = [
        {"label": c["label"], "size": c["size"], "sample_titles": c["sample_titles"]}
        for c in communities if c["size"] <= 2
    ]
    return {
        "orphans": orphans[:20],
        "orphan_count": len(orphans),
        "small_topics": small_topics,
    }


def node_community_map(G, communities: List[dict]) -> Dict[str, int]:
    """从 detect_communities 结果反推 node_id → 社区编号,供 surprising_links 用。"""
    m: Dict[str, int] = {}
    for c in communities:
        for nid in c["members"]:
            m[nid] = c["id"]
    return m
