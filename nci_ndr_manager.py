"""NCI NDR Analysis.

Design doc is available here:

https://docs.google.com/document/d/
1Iw8YxrXPSbSp5TemRo-mbfvxDiTpdCKqRrW1terp2gE/edit

"""
import argparse
import datetime
import logging
import os
import pathlib
import queue
import sqlite3
import sys
import time
import zipfile

import flask
from osgeo import gdal
import ecoshard
import requests
import shapely.strtree
import shapely.wkb
import taskgraph

WATERSHEDS_URL = (
    'https://storage.googleapis.com/nci-ecoshards/'
    'watersheds_globe_HydroSHEDS_15arcseconds_'
    'blake2b_14ac9c77d2076d51b0258fd94d9378d4.zip')

COUNTRY_BORDERS_URL = (
    'https://storage.googleapis.com/nci-ecoshards/'
    'world_borders_md5_c8dd971a8a853b2f3e1d3801b9747d5f.gpkg')

WORKSPACE_DIR = 'workspace_manager'
ECOSHARD_DIR = os.path.join(WORKSPACE_DIR, 'ecoshards')
CHURN_DIR = os.path.join(WORKSPACE_DIR, 'churn')
STATUS_DATABASE_PATH = os.path.join(CHURN_DIR, 'status_database.db')
logging.basicConfig(
    level=logging.DEBUG,
    format=(
        '%(asctime)s (%(relativeCreated)d) %(levelname)s %(name)s'
        ' [%(funcName)s:%(lineno)d] %(message)s'),
    stream=sys.stdout)
LOGGER = logging.getLogger(__name__)
WORKER_QUEUE = queue.Queue()
APP = flask.Flask(__name__)


def main(n_workers):
    """Entry point."""
    for dir_path in [WORKSPACE_DIR, ECOSHARD_DIR, CHURN_DIR]:
        try:
            os.makedirs(dir_path)
        except OSError:
            pass

    task_graph = taskgraph.TaskGraph(WORKSPACE_DIR, n_workers)
    # download countries
    country_borders_path = os.path.join(
        ECOSHARD_DIR, os.path.basename(COUNTRY_BORDERS_URL))
    country_fetch_task = task_graph.add_task(
        func=ecoshard.download_url,
        args=(COUNTRY_BORDERS_URL, country_borders_path),
        target_path_list=[country_borders_path],
        task_name='download country borders')

    # download watersheds
    watersheds_zip_path = os.path.join(
        ECOSHARD_DIR, os.path.basename(WATERSHEDS_URL))
    LOGGER.debug(
        'scheduing download of watersheds: %s', WATERSHEDS_URL)
    watersheds_zip_fetch_task = task_graph.add_task(
        func=ecoshard.download_url,
        args=(WATERSHEDS_URL, watersheds_zip_path),
        target_path_list=[watersheds_zip_path],
        task_name='download watersheds zip')
    watersheds_unzip_dir = os.path.join(
        CHURN_DIR, os.path.basename(watersheds_zip_path.replace('.zip', '')))
    unzip_token_path = os.path.join(
        CHURN_DIR, '%s.UNZIPTOKEN' % os.path.basename(watersheds_unzip_dir))
    LOGGER.debug(
        'scheduing unzip of: %s', watersheds_zip_path)
    unzip_watersheds_task = task_graph.add_task(
        func=unzip_file,
        args=(watersheds_zip_path, watersheds_unzip_dir, unzip_token_path),
        target_path_list=[unzip_token_path],
        dependent_task_list=[watersheds_zip_fetch_task],
        task_name='unzip %s' % watersheds_zip_path)

    database_complete_token_path = os.path.join(
        CHURN_DIR, 'create_status_database.COMPLETE')

    create_status_database_task = task_graph.add_task(
        func=create_status_database,
        args=(
            STATUS_DATABASE_PATH, watersheds_unzip_dir, country_borders_path,
            database_complete_token_path),
        target_path_list=[database_complete_token_path],
        dependent_task_list=[country_fetch_task, unzip_watersheds_task],
        task_name='create status database')

    task_graph.join()
    task_graph.close()

    # callback_url = flask.url_for(
    #     'processing_complete', _external=True,
    #     watershed_basename=watershed_basename, fid=fid)
    # data_payload = {
    #     'watershed_path': watershed_shape_path,
    #     'fid': fid,
    #     'bucket_id': 'NOBUCKET',
    #     'callback_url': callback_url,
    # }
    # while True:
    #     try:
    #         LOGGER.debug(
    #             'fetching a worker for %s:%s', watershed_shape_path,
    #             fid)
    #         worker_ip_port = WORKER_QUEUE.get()
    #         worker_rest_url = (
    #             'http://%s/api/v1/run_ndr' % worker_ip_port)
    #         response = requests.post(
    #             worker_rest_url, data=data_payload)
    #         if response.ok:
    #             WORKER_QUEUE.put(worker_ip_port)
    #             break
    #     except Exception:
    #         LOGGER.exception('something bad happened')


def unzip_file(zip_path, target_directory, token_file):
    """Unzip contents of `zip_path` into `target_directory`."""
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(target_directory)
    with open(token_file, 'w') as token_file:
        token_file.write(str(datetime.datetime.now()))


def create_status_database(
        database_path, watersheds_dir_path, country_borders_path,
        complete_token_path):
    """Create the initial database that monitors execution status.

    Parameters:
        database_path (str): path to SQLite database that's created by this
            call.
        watersheds_dir_path (str): path to a directory containing .shp files
            that correspond to global watersheds.
        country_borders_path (str): path to a vector containing polygon country
            shapes, used to identify which watersheds are in which countries.
        complete_token_path (str): path to a file that's written if the
            entire initialization process has completed successfully.

    Returns:
        None.

    """
    LOGGER.debug('launching create_status_database')
    create_database_sql = (
        """
        CREATE TABLE job_status (
            watershed_basename TEXT NOT NULL,
            fid INT NOT NULL,
            watershed_area_deg REAL NOT NULL,
            job_status TEXT NOT NULL,
            country_list TEXT NOT NULL,
            workspace_url TEXT NOT NULL);
        """)
    if os.path.exists(database_path):
        os.remove(database_path)
    connection = sqlite3.connect(database_path)
    cursor = connection.cursor()
    cursor.executescript(create_database_sql)

    world_borders_vector = gdal.OpenEx(
        country_borders_path, gdal.OF_VECTOR)
    world_borders_layer = world_borders_vector.GetLayer()
    world_border_polygon_list = []
    for feature in world_borders_layer:
        geom = shapely.wkb.loads(
            feature.GetGeometryRef().ExportToWkb())
        geom.country_name = feature.GetField('NAME')
        world_border_polygon_list.append(geom)

    str_tree = shapely.strtree.STRtree(world_border_polygon_list)
    insert_query = (
        'INSERT INTO job_status('
        'watershed_basename, fid, watershed_area_deg, job_status, '
        'country_list, workspace_url) VALUES (?, ?, ?, ?, ?, ?)')

    for watershed_shape_path in [str(p) for p in pathlib.Path(
            watersheds_dir_path).rglob('*.shp')]:
        LOGGER.debug('watershed shape path: %s', watershed_shape_path)
        watershed_vector = gdal.OpenEx(watershed_shape_path, gdal.OF_VECTOR)
        watershed_layer = watershed_vector.GetLayer()
        LOGGER.debug('processing watershed %s', watershed_shape_path)
        watershed_basename = os.path.splitext(
            os.path.basename(watershed_shape_path))[0]
        job_status_list = []
        last_time = time.time()
        for index, watershed_feature in enumerate(watershed_layer):
            if time.time() - last_time > 5.0:
                last_time = time.time()
                LOGGER.debug(
                    '%.2f%% complete',
                    100. * index/float(watershed_layer.GetFeatureCount()))
            fid = watershed_feature.GetFID()
            watershed_geom = shapely.wkb.loads(
                watershed_feature.GetGeometryRef().ExportToWkb())
            name_list = []
            for intersect_geom in str_tree.query(watershed_geom):
                if intersect_geom.intersects(watershed_geom):
                    name_list.append(intersect_geom.country_name)
            country_names = ','.join(name_list)
            job_status_list.append(
                (watershed_basename, fid, watershed_geom.area, 'PRESCHEDULED',
                 country_names, None))
            if index % 10000 == 0:
                LOGGER.debug(
                    'every 100 inserting %s watersheds into DB',
                    watershed_basename)
                cursor.executemany(insert_query, job_status_list)
                job_status_list = []
        if job_status_list:
            LOGGER.debug('inserting %s watersheds into DB', watershed_basename)
            cursor.executemany(insert_query, job_status_list)
    LOGGER.debug('all done with watersheds')

    cursor.close()
    connection.commit()

    with open(complete_token_path, 'w') as token_file:
        token_file.write(str(datetime.datetime.now()))


@APP.route('/api/v1/processing_complete', methods=['POST'])
def processing_complete(watershed_basename, fid):
    """Invoked when processing is complete for given watershed.

    Body of the post includs a url to the stored .zip file of the archive.

    Returns
        None.

    """
    LOGGER.debug('updating %s:%d complete', watershed_basename, fid)
    payload = flask.request.get_json()
    workspace_url = payload['workspace_url']
    while True:
        try:
            connection = sqlite3.connect(STATUS_DATABASE_PATH)
            cursor = connection.cursor()
            cursor.execute(
                'UPDATE job_status set workspace_url=? '
                'WHERE watershed_basename=? AND fid=?',
                (workspace_url, watershed_basename, fid))
            cursor.commit()
            cursor.close()
            break
        except Exception:
            LOGGER.exception(
                'exception when inserting %s:%d, trying again',
                watershed_basename, fid)
    LOGGER.debug('%s:%d complete', watershed_basename, fid)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='NCI NDR Analysis.')
    parser.add_argument(
        'n_workers', type=int, default=-1,
        help='number of taskgraph workers to create')

    args = parser.parse_args()
    WORKER_QUEUE.put('localhost:8888')
    main(args.n_workers)
    # TODO: for debugging
