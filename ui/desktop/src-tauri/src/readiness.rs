use std::fmt;

use serde::Deserialize;

pub const LOOPBACK_HOST: &str = "127.0.0.1";
pub const MAX_READINESS_BYTES: usize = 8 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadyDocument {
    #[serde(rename = "type")]
    record_type: String,
    host: String,
    port: u16,
}

pub struct ReadyRecord {
    port: u16,
}

impl ReadyRecord {
    pub fn port(&self) -> u16 {
        self.port
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReadinessError;

impl fmt::Display for ReadinessError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("sidecar readiness failed")
    }
}

impl std::error::Error for ReadinessError {}

pub fn parse_ready(input: &[u8]) -> Result<ReadyRecord, ReadinessError> {
    if input.len() > MAX_READINESS_BYTES {
        return Err(ReadinessError);
    }
    let document: ReadyDocument = serde_json::from_slice(input).map_err(|_| ReadinessError)?;
    if document.record_type != "server-ready"
        || document.host != LOOPBACK_HOST
        || document.port == 0
    {
        return Err(ReadinessError);
    }
    Ok(ReadyRecord {
        port: document.port,
    })
}

enum AccumulatorState {
    Collecting(Vec<u8>),
    Complete,
    Failed,
}

pub struct ReadinessAccumulator {
    state: AccumulatorState,
}

impl ReadinessAccumulator {
    pub fn new() -> Self {
        Self {
            state: AccumulatorState::Collecting(Vec::new()),
        }
    }

    pub fn push(&mut self, chunk: &[u8]) -> Result<Option<ReadyRecord>, ReadinessError> {
        let AccumulatorState::Collecting(buffer) = &mut self.state else {
            return match self.state {
                AccumulatorState::Complete => Ok(None),
                AccumulatorState::Failed => Err(ReadinessError),
                AccumulatorState::Collecting(_) => unreachable!(),
            };
        };

        let record_fragment = chunk
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(chunk, |newline| &chunk[..newline]);
        if buffer.len() + record_fragment.len() > MAX_READINESS_BYTES {
            self.state = AccumulatorState::Failed;
            return Err(ReadinessError);
        }
        buffer.extend_from_slice(record_fragment);

        if record_fragment.len() == chunk.len() {
            return Ok(None);
        }

        let parsed = parse_ready(buffer);
        self.state = if parsed.is_ok() {
            AccumulatorState::Complete
        } else {
            AccumulatorState::Failed
        };
        parsed.map(Some)
    }
}

impl Default for ReadinessAccumulator {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_exact_ready_record() {
        let record = parse_ready(br#"{"type":"server-ready","host":"127.0.0.1","port":49152}"#)
            .expect("exact readiness record must parse");

        assert_eq!(record.port(), 49_152);
    }

    #[test]
    fn rejects_unknown_fields_and_trailing_data() {
        assert!(
            parse_ready(
                br#"{"type":"server-ready","host":"127.0.0.1","port":49152,"extra":true}"#,
            )
            .is_err()
        );
        assert!(
            parse_ready(br#"{"type":"server-ready","host":"127.0.0.1","port":49152} trailing"#,)
                .is_err()
        );
    }

    #[test]
    fn rejects_malformed_json() {
        assert!(parse_ready(br#"{"type":"server-ready""#).is_err());
    }

    #[test]
    fn rejects_wrong_record_type() {
        assert!(parse_ready(br#"{"type":"not-ready","host":"127.0.0.1","port":49152}"#).is_err());
    }

    #[test]
    fn rejects_non_loopback_host() {
        assert!(
            parse_ready(br#"{"type":"server-ready","host":"localhost","port":49152}"#).is_err()
        );
    }

    #[test]
    fn rejects_zero_port() {
        assert!(parse_ready(br#"{"type":"server-ready","host":"127.0.0.1","port":0}"#).is_err());
    }

    #[test]
    fn accumulator_assembles_fragments_and_stops_parsing_after_first_newline() {
        let mut accumulator = ReadinessAccumulator::new();
        assert!(
            accumulator
                .push(br#"{"type":"server-ready","host":"127."#)
                .expect("first fragment must be accepted")
                .is_none()
        );

        let record = accumulator
            .push(
                br#"0.0.1","port":49152}
ignored post-readiness stdout"#,
            )
            .expect("completed record must parse")
            .expect("newline must complete readiness");

        assert_eq!(record.port(), 49_152);
        assert!(
            accumulator
                .push(b"not readiness protocol\n")
                .expect("post-readiness stdout must be ignored")
                .is_none()
        );
    }

    #[test]
    fn accumulator_rejects_partial_or_complete_records_over_eight_kibibytes() {
        let mut fragmented = ReadinessAccumulator::new();
        assert!(
            fragmented
                .push(&vec![b' '; MAX_READINESS_BYTES])
                .expect("a partial record at the cap remains pending")
                .is_none()
        );
        assert!(fragmented.push(b"x").is_err());

        let mut complete = vec![b' '; MAX_READINESS_BYTES + 1];
        complete.push(b'\n');
        assert!(ReadinessAccumulator::new().push(&complete).is_err());
    }

    #[test]
    fn parser_rejects_an_oversized_record_even_when_json_is_otherwise_valid() {
        let mut oversized = vec![b' '; MAX_READINESS_BYTES + 1];
        oversized.extend_from_slice(br#"{"type":"server-ready","host":"127.0.0.1","port":49152}"#);

        assert!(parse_ready(&oversized).is_err());
    }
}
