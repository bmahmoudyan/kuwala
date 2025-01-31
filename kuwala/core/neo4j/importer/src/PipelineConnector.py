import Neo4jConnection as Neo4jConnection
import time
from pyspark.sql import DataFrame
from pyspark.sql.functions import col, concat, when
from pyspark.sql.types import LongType
from python_utils.src.spark_udfs import build_poi_id_based_on_confidence


def add_constraints():
    Neo4jConnection.connect_to_graph()
    Neo4jConnection.query_graph('CREATE CONSTRAINT poi IF NOT EXISTS ON (p:Poi) ASSERT p.id IS UNIQUE')
    Neo4jConnection.close_connection()


# Create relationships from high resolution
# H3 indexes to lower resolution H3 indexes
def connect_h3_indexes():
    start_time = time.time()

    Neo4jConnection.connect_to_graph()

    query_resolutions = '''
        MATCH (h:H3Index)
        WITH DISTINCT h.resolution AS resolutions
        ORDER BY resolutions DESC
        RETURN resolutions
    '''
    resolutions = Neo4jConnection.query_graph(query_resolutions)
    resolutions = resolutions[0]  # Query returns a tuple with the results at the first index
    resolutions = [i for i in resolutions if i[0] is not None]

    # Find parents of children and connect them
    for i, r in enumerate(resolutions):
        if i < len(resolutions) - 1:
            query_connect_to_parent = f'''
                MATCH (h1:H3Index)
                WHERE h1.resolution = {r[0]} 
                MATCH (h2:H3Index)
                WHERE h2.h3Index = io.kuwala.h3.h3ToParent(h1.h3Index, {resolutions[i + 1][0]})
                MERGE (h1)-[:CHILD_OF]->(h2)
            '''

            Neo4jConnection.query_graph(query_connect_to_parent)

    Neo4jConnection.close_connection()

    end_time = time.time()

    print(f'Connected H3 indexes in {round(end_time - start_time)} s')


def add_pois(df):
    query = '''
        MERGE (p:Poi { id: event.id })
        WITH event, p
        MATCH (h:H3Index { h3Index: event.h3Index })
        MERGE (p)-[:LOCATED_AT]->(h)
    '''

    Neo4jConnection.write_df_to_neo4j_with_override(df.repartition(1), query)


def connect_osm_pois(df):
    query = '''
        MATCH (p:Poi { id: event.id })
        WITH event, p
        MATCH (po:PoiOSM { id: event.osmId })
        MERGE (po)-[:BELONGS_TO]->(p)
    '''

    Neo4jConnection.write_df_to_neo4j_with_override(df.repartition(1), query)


def connect_google_pois(df):
    query_belongs_to = '''
        MATCH (p:Poi { id: event.id })
        WITH event, p
        MATCH (pg:PoiGoogle { id: event.googleId })
        MERGE (pg)-[:BELONGS_TO { confidence: event.confidence }]->(p)
    '''
    query_inside_of = '''
        MATCH (p:Poi)<-[:BELONGS_TO]-(:PoiGoogle { id: event.insideOf })
        WITH event, p
        MATCH (pg:PoiGoogle { id: event.googleId })
        MERGE (pg)-[:INSIDE_OF]->(p)
    '''

    Neo4jConnection.write_df_to_neo4j_with_override(df.repartition(1), query_belongs_to)
    Neo4jConnection.write_df_to_neo4j_with_override(df.filter(col('insideOf').isNotNull()).repartition(1), query_inside_of)


# Create one single POI node combining OSM and Google
def connect_pois(df_osm: DataFrame, df_google: DataFrame):
    start_time = time.time()
    df_osm = df_osm.select('id', 'h3_index') \
        .withColumnRenamed('h3_index', 'h3IndexOsm') \
        .withColumnRenamed('id', 'osmId')
    df_google = df_google \
        .withColumn('osmId', df_google['osmId'].cast(LongType())) \
        .withColumn('osmId', concat('type', 'osmId')) \
        .select('id', 'osmId', 'confidence', 'h3Index', 'insideOf') \
        .withColumnRenamed('id', 'googleId') \
        .withColumnRenamed('h3Index', 'h3IndexGoogle')

    df_pois = df_osm.join(df_google, on=['osmId'], how='left') \
        .withColumn(
            'id',
            build_poi_id_based_on_confidence(col('confidence'), col('h3IndexGoogle'), col('h3IndexOsm'), col('osmId'))
        ) \
        .withColumn('h3Index', when(col('confidence') >= 0.9, col('h3IndexGoogle')).otherwise(col('h3IndexOsm')))
    pois = df_pois.select('id', 'h3Index').distinct()
    osm_pois = df_pois.select('id', 'osmId')
    google_pois = df_pois.filter(col('confidence').isNotNull()).select('id', 'confidence', 'googleId', 'insideOf')

    add_constraints()
    add_pois(pois)
    connect_osm_pois(osm_pois)
    connect_google_pois(google_pois)

    end_time = time.time()

    print(f'Connected POIs in {round(end_time - start_time)} s')
