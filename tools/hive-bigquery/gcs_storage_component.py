"""Module to handle Google Cloud Storage related utilities"""

import logging
import os
import time

from google.cloud import storage

from utilities import calculate_time, get_random_string, execute_command, \
    print_and_log
from gcp_service import GCPService

logger = logging.getLogger('Hive2BigQuery')


class GCSStorageComponent(GCPService):
    """GCS component to handle functions related to it

    Has utilities which do GCS operations using the GCS client, such as
    uploading file, getting the bucket location, checking whether a file
    exists, copying the staged data to GCS etc.

    Attributes:
        project_id: GCP Project ID
        client: GCS Client of class google.cloud.storage.client.Client
    """

    def __init__(self, project_id):

        logger.debug("Initializing GCS Component")
        super(GCSStorageComponent, self).__init__(project_id)

    def get_client(self):
        """Creates BigQuery client

        Returns:
            google.cloud.storage.client.Client: GCS client
        """

        logger.debug("Getting GCS client")
        return storage.Client(project=self.project_id)

    def upload_file(self, bucket_name, file_name, blob_name):
        """Uploads local file to GCS bucket

        Args:
            bucket_name (str): GCS bucket name
            file_name (str): Local file name to be uploaded
            blob_name (str): Destination path of the object
        """

        bucket = self.client.get_bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(file_name)
        uri = 'gs://' + bucket_name + '/' + blob_name
        return uri

    def delete_file(self, bucket_name, file_name):
        """Deletes GCS file

        Args:
            bucket_name (str): GCS bucket name
            file_name (str): Complete GCS URI of the object or simply path
        """

        bucket = self.client.get_bucket(bucket_name)
        if file_name.startswith('gs://' + bucket_name + '/'):
            blob_name = file_name.split('gs://' + bucket_name + '/')[1]
        else:
            blob_name = file_name
        blob = bucket.blob(blob_name)
        blob.delete()
        logger.debug('GCS File %s deleted in %s bucket', blob_name, bucket_name)

    def check_bucket_exists(self, bucket_name):
        """Checks whether GCS bucket exists

        Args:
            bucket_name (str): GCS bucket name
        """

        try:
            self.client.get_bucket(bucket_name)
            return True
        except Exception as error:
            logger.error(error)
            return False

    def get_bucket_location(self, bucket_name):
        """Returns the bucket location

        Args:
            bucket_name (str): GCS bucket name
        """

        return self.client.get_bucket(bucket_name).location

    def check_file_exists(self, bucket_name, gcs_uri):
        """Checks whether file is present in GCS bucket

        Args:
            bucket_name (str): GCS bucket name
            gcs_uri (str): GCS URI of the file
        """

        bucket = self.client.get_bucket(bucket_name)
        blob_name = gcs_uri.split('gs://' + bucket_name + '/')[1]
        blob = bucket.get_blob(blob_name)
        if blob:
            return True
        logger.debug("File %s doesn't exist", gcs_uri)
        return False

    def stage_to_gcs(self, mysql_component, bq_component, hive_table_model,
                     bq_table_model, gcs_bucket_name):
        """Copies staged files to GCS

        Queries the tracking table, fetches information about the files to
        copy to GCS, runs a distcp job to copy multiple files, and checks
        whether the files have been successfully copied. If copied
        successfully, updates the gcs_copy_status to 'DONE' else retries
        copying.

        Args:
            mysql_component (:class:`MySQLComponent`): Instance of
                MySQLComponent to connect to MySQL
            bq_component (:class:`BigQueryComponent`): Instance of
                BigQueryComponent to do BigQuery operations
            hive_table_model (:class:`HiveTableModel`): Wrapper to Hive table
                details
            bq_table_model (:class:`BigQueryTableModel`): Wrapper to BigQuery
                table details
            gcs_bucket_name (str): GCS bucket name
        """

        logger.debug(
            "Fetching information about files to copy to GCS from tracking "
            "table...")
        select_query = "SELECT table_name,file_path FROM %s WHERE " \
                       "gcs_copy_status='TODO'" % (
                           hive_table_model.tracking_table_name)
        results = mysql_component.execute_query(select_query)

        if not results:
            logger.debug("No file paths to copy to GCS")

        while results:
            file_info = {}
            for row in results:
                source_location = row[1]
                file_name = source_location.split('/')[-1]
                if file_name not in file_info.keys():
                    file_info[file_name] = source_location
            source_locations = ' '.join(file_info.values())
            filename = "file_info.json"
            # Dictionary of file names and their locations
            with open(filename, "w") as file_content:
                file_content.write(str(file_info))

            target_blob = "BQ_staging/{}/{}/{}/".format(
                hive_table_model.database_name,
                hive_table_model.table_name.lower(), get_random_string())
            # Uploads file to create a folder like structure in GCS
            self.upload_file(gcs_bucket_name, filename, target_blob + filename)
            os.remove(filename)

            target_folder_location = "gs://{}/{}".format(gcs_bucket_name,
                                                         target_blob)

            logger.debug(
                "Copying data from location %s to GCS Staging location %s "
                "....", source_locations, target_folder_location)
            # Hadoop distcp command to copy multiple files in one operation
            cmd_copy_gcs = ['hadoop', 'distcp'] + file_info.values() + [
                target_folder_location]
            print_and_log("Running " + " ".join(cmd_copy_gcs))

            start = time.time()
            execute_command(cmd_copy_gcs)
            end = time.time()
            time_distcp_gcs = calculate_time(start, end)
            logger.debug("Time taken - %s", time_distcp_gcs)

            # Iterates though the dict and checks whether the distcp
            # operation is successful or partially completed
            for file_name, source_location in file_info.iteritems():
                target_file_location = target_folder_location + file_name
                # Checks whether the copied file is present at the GCS location
                if self.check_file_exists(gcs_bucket_name,
                                          target_file_location):
                    logger.error(
                        "Finished copying data from location %s to GCS "
                        "Staging location %s", source_location,
                        target_file_location)
                    query = "UPDATE %s SET gcs_copy_status='DONE'," \
                            "gcs_file_path='%s' WHERE file_path='%s'" % (
                                hive_table_model.tracking_table_name,
                                target_file_location, source_location)
                    mysql_component.execute_transaction(query)
                    logger.debug(
                        "Updated GCS copy status TODO --> DONE for file path %s",
                        source_location)
                else:
                    logger.error(
                        "Failed copying data from location %s to GCS Staging "
                        "location %s", source_location, target_file_location)
            # Starts loading the copied files
            bq_component.load_gcs_to_bq(mysql_component, hive_table_model,
                                        bq_table_model)

            results = mysql_component.execute_query(select_query)