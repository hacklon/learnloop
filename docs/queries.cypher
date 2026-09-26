// LearnLoop — demo queries for Neo4j Aura (Query or Explore)

// 1. The whole context graph for one student (all memory layers)
MATCH p=(:Student {name:'Aryan'})-[*1..3]-()
RETURN p LIMIT 150;

// 2. What is Aryan forgetting, and which prerequisites explain it?
MATCH (s:Student {name:'Aryan'})-[k:KNOWS]->(c:Concept)
OPTIONAL MATCH (p:Concept)-[:PREREQUISITE_OF]->(c)
RETURN c.name AS concept, k.stability AS stability, k.reviews AS reviews,
       k.correct AS correct, collect(p.name) AS prerequisites
ORDER BY stability;

// 3. Teacher's notes, grounded to concepts (GraphRAG path)
MATCH p=(:Class {name:'8A'})-[:HAD_LESSON]->(:Lesson)-[:HAS_NOTE]->(:Note)-[:EXPLAINS]->(:Concept)
RETURN p;

// 4. Which students have not revised which concepts from class 8A?
MATCH (:Class {name:'8A'})-[:HAD_LESSON]->(:Lesson)-[:INCLUDES]->(c:Concept),
      (st:Student)-[:ENROLLED_IN]->(:Class {name:'8A'})
WHERE NOT (st)-[:KNOWS]->(c)
RETURN st.name AS student, collect(DISTINCT c.name) AS not_revised;

// 5. Topic-wise view: subject -> concepts -> students who know them
MATCH p=(t:Topic)<-[:PART_OF]-(c:Concept)<-[:KNOWS]-(s:Student)
RETURN p LIMIT 150;

// 6. Reasoning memory: which strategy works for which student?
MATCH (s:Student)-[:HAS_TRACE]->(t:ReasoningTrace)
WHERE t.success IS NOT NULL
RETURN s.name AS student, t.strategy AS strategy, count(t) AS tries,
       sum(CASE WHEN t.success THEN 1 ELSE 0 END) AS worked
ORDER BY student, worked DESC;

// 7. Term-long answer history: every attempt, by week
MATCH (s:Student {name:'Aryan'})-[:ATTEMPTED]->(a:Attempt)-[:ON]->(c:Concept)
RETURN c.name AS concept, a.kind AS kind, a.correct AS correct, a.ts AS ts
ORDER BY ts;

// 8. Subject -> chapter -> lesson structure for a class
MATCH p=(:Class {name:'8A'})-[:STUDIES]->(:Subject)-[:HAS_CHAPTER]->(:Chapter)<-[:PART_OF_CHAPTER]-(:Lesson)
RETURN p;
