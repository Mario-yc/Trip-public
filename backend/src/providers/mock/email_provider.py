from dataclasses import dataclass


@dataclass
class SimulatedEmailResult:
    status: str
    message: str


class SimulatedEmailProvider:
    name = "mock-email-provider"

    def schedule(self, email_address: str, subject: str, body: str) -> SimulatedEmailResult:
        return SimulatedEmailResult(
            status="scheduled",
            message=f"Simulated email scheduled for {email_address}: {subject}",
        )
