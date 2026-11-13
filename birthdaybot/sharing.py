"""Independent member sharing permissions, with legacy consent compatibility."""

from dataclasses import dataclass

FLAGS = ("list_public", "announce", "dm_enabled", "mention_enabled")
LABELS = ("Public list", "Server posts", "Birthday DMs", "Post mentions")


@dataclass(frozen=True)
class Sharing:
    list_public: bool
    announce: bool
    dm_enabled: bool
    mention_enabled: bool

    def __post_init__(self):
        if any(type(value) is not bool for value in self.values()):
            raise ValueError("Sharing permissions must be On or Off.")

    def values(self):
        return tuple(getattr(self, key) for key in FLAGS)

    @property
    def enabled(self):
        return self.list_public or self.announce or self.dm_enabled

    @classmethod
    def all(cls, enabled):
        if type(enabled) not in (bool, int) or enabled not in (0, 1):
            raise ValueError("Sharing permissions must be On or Off.")
        return cls(*(bool(enabled) for _ in FLAGS))

    @classmethod
    def from_record(cls, record):
        return cls(
            *(
                bool(record[key]) if key in record.keys() else bool(record["consent"])
                for key in FLAGS
            )
        )

    def record_values(self):
        return dict(zip(FLAGS, self.values(), strict=True))
