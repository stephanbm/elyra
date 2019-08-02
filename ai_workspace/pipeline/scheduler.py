import json
import kfp
import os
import tarfile

from datetime import datetime
from minio import Minio
from minio.error import (ResponseError,
                         BucketAlreadyOwnedByYou,
                         BucketAlreadyExists)
from notebook.base.handlers import IPythonHandler


class SchedulerHandler(IPythonHandler):

    """REST-ish method calls to execute pipelines as batch jobs"""
    def get(self):
        msg_json = dict(title="Operation not supported.")
        self.write(msg_json)
        self.flush()

    def post(self, *args, **kwargs):
        self.log.debug("Pipeline SchedulerHandler now executing post request")

        """Upload endpoint"""
        url = 'http://weakish1.fyre.ibm.com:32488/pipeline'
        endpoint = 'http://weakish1.fyre.ibm.com:30427'
        minio_username = 'minio'
        minio_password = 'minio123'
        bucket_name = 'lresende'

        options = self.get_json_body()

        self.log.debug("JSON options: %s", options)

        # Iterate through the components and create a list of input components
        links = {}
        labels = {}
        docker_images = {}
        for component in options['pipeline_data']['nodes']:
            # Set up dictionary to track node id's of inputs
            links[component['id']] = []
            if 'links' in component['inputs'][0]:
                for link in component['inputs'][0]['links']:
                    links[component['id']].append(link['node_id_ref'])

            # Set up dictionary to link component id's to
            # component names (which are ipynb filenames)

            # Component id's are generated by CommonCanvas
            labels[component['id']] = component['app_data']['notebook']
            docker_images[component['id']] = component['app_data']['docker_image']

        # Initialize minioClient with an endpoint and access/secret keys.
        minio_client = Minio('weakish1.fyre.ibm.com:30427',
                             access_key=minio_username,
                             secret_key=minio_password,
                             secure=False)

        # Make a bucket with the make_bucket API call.
        try:
            if not minio_client.bucket_exists(bucket_name):
                minio_client.make_bucket(bucket_name)
        except BucketAlreadyOwnedByYou:
            self.log.warning("Minio bucket already owned by you", exc_info=True)
            pass
        except BucketAlreadyExists:
            self.log.warning("Minio bucket already exists", exc_info=True)
            pass
        except ResponseError:
            self.log.error("Minio error", exc_info=True)
            raise

        def cc_pipeline():
            # Create dictionary that maps component Id to its ContainerOp instance
            notebookops = {}
            # Create component for each node from CommonCanvas
            for componentId, inputs in links.items():
                notebookPath = labels[componentId]
                name = os.path.basename(notebookPath).split(".")[0]
                output_filename = options['pipeline_name'] + datetime.now().strftime("%m%d%H%M%S") + ".tar.gz"
                extracted_dir_from_tar = "jupyter-work-dir"
                docker_image = docker_images[componentId]

                self.log.debug("Creating pipeline component :\n "
                               "componentID : %s \n "
                               "inputs : %s \n "
                               "name : %s \n "
                               "output_filename : %s \n "
                               "extracted_dir_from_tar : %s"
                               "docker image : %s \n ",
                               componentId,
                               inputs,
                               name,
                               output_filename,
                               extracted_dir_from_tar,
                               docker_image)

                notebookops[componentId] = \
                    kfp.dsl.ContainerOp(name=name,
                                        image=docker_image,
                                        command=['sh', '-c'],
                                        arguments=['pip install papermill && '
                                                   'apt install -y wget &&'
                                                   'wget https://dl.min.io/client/mc/release/linux-amd64/mc && '
                                                   'chmod +x mc && '
                                                   './mc config host add aiworkspace '+endpoint+' '+minio_username+' '+minio_password+' && '
                                                   './mc cp aiworkspace/'+bucket_name+'/'+output_filename+ ' . && '
                                                   'mkdir -p '+extracted_dir_from_tar+' && '
                                                   'cd '+extracted_dir_from_tar+' && '
                                                   'tar -zxvf ../'+output_filename+' --strip 1 && '
                                                   'echo $(pwd) && '
                                                   'ls -la && '
                                                   'papermill '+name+'.ipynb '+name+'_output.ipynb && '
                                                   'jupyter nbconvert --to html '+name+'_output.ipynb --output '+name+'_output.html && '
                                                   '../mc cp '+name+'_output.ipynb aiworkspace/'+bucket_name+'/'+name+'_output.ipynb && '
                                                   '../mc cp '+name+'_output.html aiworkspace/'+bucket_name+'/'+name+'_output.html'
                                        ])

                self.log.info("NotebookOp Created for Component %s", componentId)

                try:
                    full_notebook_path = os.path.join(os.getcwd(), notebookPath)
                    notebook_work_dir = os.path.dirname(full_notebook_path)

                    self.log.debug("Creating TAR archive %s with contents from %s", output_filename, notebook_work_dir)

                    with tarfile.open(output_filename, "w:gz") as tar:
                        tar.add(notebook_work_dir, arcname=output_filename)

                    self.log.info("TAR archive %s created", output_filename)

                    minio_client.fput_object(bucket_name=bucket_name,
                                             object_name=output_filename,
                                             file_path=output_filename)

                    self.log.debug("TAR archive %s pushed to bucket : %s ", output_filename, bucket_name)

                except ResponseError:
                    self.log.error("ERROR : From object storage", exc_info=True)

            # Add order based on list of inputs for each component.
            for componentId, inputs in links.items():
                for inputComponentId in inputs:
                    notebookops[componentId].after(notebookops[inputComponentId])

            self.log.info("Pipeline dependencies are set")

        pipeline_name = options['pipeline_name']+datetime.now().strftime("%m%d%H%M%S")

        local_working_dir = "pipeline_files"
        self.log.info("Pipeline : %s", pipeline_name)
        self.log.debug("Creating local directory %s", local_working_dir)

        if not os.path.exists(local_working_dir):
            os.mkdir(local_working_dir)

        pipeline_path = local_working_dir+'/'+pipeline_name+'.tar.gz'

        # Compile the new pipeline
        kfp.compiler.Compiler().compile(cc_pipeline,pipeline_path)

        self.log.info("Kubeflow Pipeline successfully compiled!")
        self.log.debug("Kubeflow Pipeline compiled pipeline placed into %s", pipeline_path)

        # Upload the compiled pipeline and create an experiment and run
        client = kfp.Client(host=url)
        kfp_pipeline = client.upload_pipeline(pipeline_path, pipeline_name)

        self.log.info("Kubeflow Pipeline successfully uploaded to : %s", url)

        client.run_pipeline(experiment_id=client.create_experiment(pipeline_name).id,
                            job_name=datetime.now().strftime("%m%d%H%M%S"),
                            pipeline_id=kfp_pipeline.id)

        self.log.info("Starting Kubeflow Pipeline Run...")

    def send_message(self, message):
        self.write(message)
        self.flush()

    def send_success_message(self, message, job_url):
        self.set_status(200)
        msg = json.dumps({"status": "ok",
                          "message": message,
                          "url": job_url})
        self.send_message(msg)

    def send_error_message(self, status_code, error_message):
        self.set_status(status_code)
        msg = json.dumps({"status": "error",
                          "message": error_message})
        self.send_message(msg)

