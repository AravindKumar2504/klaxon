"""The live patient: the payments system as real services (week 1).

Four Python services, run by Docker Compose next to PostgreSQL, Redis and Kafka:

    client -> payments API -> fake provider (charges, keeps its own records)
                                   |
                                   | signed webhook, retried until acknowledged
                                   v
              ledger consumer <- Kafka <- webhook receiver
                    |
                    v
              PostgreSQL ledger  <-- the money check reads it (read-only role)

All data is synthetic and the provider is a simulator. Nothing here talks to a
real payment company.
"""
