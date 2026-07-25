.pipeline as $pipeline
| {
    pipeline: (
      $pipeline
      | .document_id = "document-stage32-synthetic"
      | .generation_id = "generation-stage32-synthetic"
      | .expected_blocks = [
          range(0; 12) as $index
          | $pipeline.expected_blocks[$index % 4]
          | .block_id = ("block-stage32-" + (($index + 1) | tostring))
        ]
      | .expected_section_ids = [
          range(0; 12) as $index
          | "section-stage32-" + (($index + 1) | tostring)
        ]
      | .windows = [
          range(0; 12) as $index
          | $pipeline.windows[$index % 4]
          | .window_id = ("window-stage32-" + (($index + 1) | tostring))
          | .sequence_no = ($index + 1)
          | .section_ids = ["section-stage32-" + (($index + 1) | tostring)]
          | .primary_block_ids = ["block-stage32-" + (($index + 1) | tostring)]
          | .offset_map[0].block_id = ("block-stage32-" + (($index + 1) | tostring))
          | .offset_map[0].block_no = ($index + 1)
        ]
      | .concurrency = 10
    )
  }
