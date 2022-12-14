"""This module has classes (Server and ServerManager) for management and autoscaling of Gameservers on AWS"""

from time import sleep
from django.conf import settings
import requests
from threading import Thread
from .aws_utils import *

class Server():
    """
    Wrapper class to store relevant info about a RUNNING ECS server instance

    Attributes:
    * task_arn: string storing aws task arn of the server instance
    * address: string storing socket address of the server instance
    * status: string from {'RUNNING', 'PENDING', 'STOPPED'}
    * available_capacity: integer measure of capacity of the server instance to handle future connections; note that this is weakly consistent and not real time
    * ready_to_close: boolean flag specifying whether the server instance can be terminated
    """

    def __init__(self, task_arn: str):
        """Waits for the task to transition to RUNNING state and then retrieves and stores relevant details about it."""
        self.task_arn: str = task_arn
        running_task_waiter(task_arn, settings.ECS_CLIENT)
        self.status: str = 'RUNNING'
        
        task_description = get_task_description(task_arn, settings.ECS_CLIENT)

        self.ec2_id: str = get_ec2_id(task_description, settings.ECS_CLIENT)

        port: str = get_exposed_port(task_description)
        ip: str = get_ip(self.ec2_id, settings.EC2_CLIENT)
        self.address: str = ip + ":" + port

        self.available_capacity: int = 0
        self.ready_to_close: bool = False
        # self.update_state()
        
    def update_state(self) -> bool:
        """
        Makes api requests to update available capacity and ready to close flag.
        Note that the api call may fail even after the task is running condition as it takes sometime for django to setup
        Returns True if the api call is successful and False otherwise.
        """
        url = "http://"+self.address+"/health/"
        try: 
            state_json: dict = requests.get(url).json()
            self.ready_to_close = state_json['ready_to_close']
            self.available_capacity = state_json['available_capacity']
            return True
        except Exception as e:
            print(e)
            return False

class ServerManagerThread(Thread):
    """
    ServerManagerThread is subclass of thread. The thread routinely updates the state of each server instance and based on this information, it upscales/downscales server instances. (Check README and code for implementation details)
    
    Attributes:
    * available_servers: list of servers available for connection
    * standby_servers: list of servers kept in standby as part of downscaling; they will be terminated when the 'ready_to_close' flag is True.
    * total_available_capacity: integer sum of available capacity of all servers
    * upscale_margin: min extra capacity maintained; one server instance is provisioned if total_available_capacity < upscale_margin
    * downscale_margin: max extra capacity maintained, one server instance is deprovisioned if total_available_capacity > downscale_margin
    * thread_sleep_time: time interval (in seconds) before the thread carries out routine updates

    This is a singleton class, meaning only one instance of Health can be created during the scope of the proram.
    """
    __shared_instance = None
    
    def __init__(self):
        if self.__shared_instance == None:
            Thread.__init__(self)
            ServerManagerThread.__shared_instance = self
            self.setDaemon(True)
            
            self.task_family = settings.SERVER_TASK_DEFINITION

            try:
                task_arns: list = get_tasks(self.task_family)
                self.available_servers: list = [Server(arn) for arn in task_arns]
            except Exception as e:
                print("Unable to get active tasks in the cluster due to the following exception")
                print(e)
                self.available_servers: list = []

            self.standby_servers: list = []

            self.total_available_capacity: int = 0
            for s in self.available_servers:
                self.total_available_capacity += s.available_capacity

            self.upscale_margin: int = settings.UPSCALE_MARGIN
            self.downscale_margin: int = settings.DOWNSCALE_MARGIN
            self.thread_sleep_time: int = settings.THREAD_SLEEP_TIME
        
        else:
            raise Exception("ServerManagerThread is Singleton class!")
    
    @staticmethod
    def get_instance():
        """Returns the singleton shared instance of this class"""
        if ServerManagerThread.__shared_instance == None:
            ServerManagerThread()
        
        return ServerManagerThread.__shared_instance

    def add_server(self) -> bool:
        """
        Attempts to add a new server instance
        Returns True if successful and False otheriwse
        """
        try:
            launch_ecs_instance()
            task = launch_task(self.task_family)
            self.available_servers.append(Server(task))
            return True
        except Exception as e:
            print(e)
            return False
    
    def remove_server(self, redundant_server: Server) -> bool:
        """
        Attempts to remove the specified server instance terminating the EC2 instance (along with task)
        Returns True if successful and False otheriwse
        """
        try:
            terminate_ec2(redundant_server.ec2_id)
            return True
        except Exception as e:
            print(e)
            return False
    
    def get_available_server(self) -> Server:
        """Return Server object of an available server instance"""
        max_available_server_index = self.get_available_server_index()
        self.available_servers[max_available_server_index].available_capacity -= 1
        return self.available_servers[max_available_server_index]
    
    def get_available_server_index(self) -> int:
        """Return the index (in self.available_servers list) of an available server instance"""
        max_available_server_index = 0

        for i in range(1, len(self.available_servers)):
            server = self.available_servers[i]
            if server.available_capacity > self.available_servers[max_available_server_index].available_capacity:
                max_available_server_index = i
        
        return max_available_server_index
    
    def get_available_servers(self) -> list:
        """Returns the list of available server instances as maintained by the class object"""
        return self.available_servers

    def run(self):
        """Main function which carries out routinely maintainance and updates in the backgorund"""

        print("Starting server management thread")

        while(True):

            new_total_available_capacity = 0 # reset total available capacity
            unresponsive_servers: list = [] # maintain a list of servers which don't respond to health checks
            for s in self.available_servers:
                if not s.update_state():
                    # server isn't responding to health check
                    s.available_capacity = 0
                    unresponsive_servers.append(s)
                new_total_available_capacity += s.available_capacity

            self.total_available_capacity = new_total_available_capacity
            
            for s in self.standby_servers:
                if not s.update_state():
                    unresponsive_servers.append(s)

            print("state updated")
            print("Total Available Capacity", end=': ')
            print(self.total_available_capacity)

            if self.total_available_capacity < self.upscale_margin:
                print("Upscale")

                if len(self.standby_servers) == 0:
                    # If there is no server in standby, launch new server instance
                    self.add_server()
                else:
                    # Move a standby server instance back as an available server
                    s = self.standby_servers.pop(0)
                    self.total_available_capacity += s.available_capacity
                    self.available_servers.append(s)
            
            elif (self.total_available_capacity > self.downscale_margin) & (len(self.available_servers)>1) :
                    print("Downscale")
                    # Move a server instance to standby
                    s = self.available_servers.pop(self.get_available_server_index())
                    self.total_available_capacity -= s.available_capacity
                    self.standby_servers.append(s)

            # Terminate standby servers with are ready to close keep only the remaining ones
            remaining_standby_servers: list = []
            for s in self.standby_servers:
                print("Removing extra servers")

                if s.ready_to_close:
                    self.remove_server(s)
                else:
                    remaining_standby_servers.append(s)
            
            self.standby_servers = remaining_standby_servers
            
            print("Ending server_update and sleeping")
            sleep(self.thread_sleep_time) # Wait a little before the next update
            # Unresponsive servers were given some time to recover
            # If they don't respond to health checks even now, then remove them
            for s in unresponsive_servers:
                if not s.update_state():
                    if s in self.available_servers:
                        self.available_servers.remove(s)
                    elif s in self.standby_servers:
                        self.standby_servers.remove(s)                    
                    self.remove_server(s)
            
        return
