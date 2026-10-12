"""The live patient: the payments system as real services (week 1; the payment confirmer, week 2).

Five Python services, run by Docker Compose next to PostgreSQL, Redis and Kafka:

    client -> payments API -> fake provider (charges, keeps its own records)
                  |                |
                  |                | signed webhook, retried until acknowledged
                  |                v
                  |          webhook receiver -> Kafka, read by two consumer groups:
                  |                                - ledger consumer: books the money
                  v                                - payment confirmer: marks payments succeeded
              PostgreSQL: payments and ledger  <-- the money check reads both (read-only role)

All data is synthetic and the provider is a simulator. Nothing here talks to a
real payment company.
"""
