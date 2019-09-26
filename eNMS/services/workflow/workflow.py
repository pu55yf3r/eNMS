from collections import defaultdict
from copy import deepcopy
from sqlalchemy import Boolean, ForeignKey, Integer
from sqlalchemy.orm import relationship
from time import sleep
from wtforms import BooleanField, HiddenField

from eNMS import app
from eNMS.database import Session
from eNMS.database.dialect import Column, MutableDict
from eNMS.database.functions import factory, fetch
from eNMS.database.associations import service_workflow_table, start_services_workflow_table
from eNMS.forms.automation import ServiceForm
from eNMS.forms.fields import MultipleInstanceField, NoValidationSelectField
from eNMS.models.automation import Service


class Workflow(Service):

    __tablename__ = "workflow"
    __mapper_args__ = {"polymorphic_identity": "workflow"}
    has_targets = Column(Boolean, default=True)
    parent_type = "service"
    id = Column(Integer, ForeignKey("service.id"), primary_key=True)
    labels = Column(MutableDict)
    services = relationship("Service", secondary=service_workflow_table, back_populates="workflows")
    edges = relationship(
        "WorkflowEdge", back_populates="workflow", cascade="all, delete-orphan"
    )
    start_services = relationship(
        "Service", secondary=start_services_workflow_table, backref="start_workflows"
    )

    def __init__(self, **kwargs):
        start, end = fetch("service", name="Start"), fetch("service", name="End")
        self.services.extend([start, end])
        super().__init__(**kwargs)
        if not kwargs.get("start_services"):
            self.start_services = [start]
        if self.name not in end.positions:
            end.positions[self.name] = (500, 0)

    def init_state(self, run):
        app.run_db[run.runtime].update(
            {"services": defaultdict(dict), "edges": defaultdict(int), "progress": defaultdict(int)}
        )

    def job(self, run, payload, device=None):
        run.set_state(progress_max=len(self.services))
        number_of_runs = defaultdict(int)
        services = list(run.start_services)
        payload = deepcopy(payload)
        visited = set()
        results = {"results": {}, "success": False, "runtime": run.runtime}
        while services:
            if run.stop:
                return results
            service = services.pop()
            if number_of_runs[service.name] >= service.maximum_runs or any(
                node not in visited
                for node, _ in service.adjacent_services(self, "source", "prerequisite")
            ):
                continue
            number_of_runs[service.name] += 1
            visited.add(service)
            app.run_db[run.runtime]["current_service"] = service.get_properties()
            skip_service = False
            if service.skip_python_query:
                skip_service = run.eval(service.skip_python_query, **locals())
            if skip_service or service.skip:
                service_results = {"success": "skipped"}
            elif device and service.python_query:
                try:
                    service_run = factory(
                        "run",
                        service=service.id,
                        workflow=self.id,
                        workflow_device=device.id,
                        parent_runtime=run.parent_runtime,
                        restart_run=run.restart_run,
                    )
                    service_run.properties = {}
                    result = service_run.run(payload)
                except Exception as exc:
                    result = {"success": False, "error": str(exc)}
                service_results = result
            else:
                service_run = factory(
                    "run",
                    service=service.id,
                    workflow=self.id,
                    parent_runtime=run.parent_runtime,
                    restart_run=run.restart_run,
                )
                service_run.properties = {"devices": [device.id]}
                Session.commit()
                service_results = service_run.run(payload)
            app.run_db[run.runtime]["services"][service.id]["success"] = service_results["success"]
            successors = []
            for successor, edge in service.adjacent_services(
                self, "destination", "success" if service_results["success"] else "failure"
            ):
                successors.append(successor)
                app.run_db[run.runtime]["edges"][edge.id] += 1
            payload[service.name] = service_results
            results["results"].update(payload)
            for successor in successors:
                services.append(successor)
                if successor == self.services[1]:
                    results["success"] = True
            if not skip_service and not service.skip:
                sleep(service.waiting_time)
        return results


class WorkflowForm(ServiceForm):
    form_type = HiddenField(default="workflow")
    has_targets = BooleanField("Has Target Devices", default=True)
    start_services = MultipleInstanceField("Workflow Entry Point(s)")
    restart_runtime = NoValidationSelectField("Restart Runtime", choices=())