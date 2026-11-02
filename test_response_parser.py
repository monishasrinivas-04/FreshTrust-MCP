
from types import SimpleNamespace
from mcp_layer.response_parser import extract_mcp_text

resource_text = "# README Test\n\nThis is actual repository content."

result = SimpleNamespace(
    content=[
        SimpleNamespace(
            type="text",
            text="successfully downloaded text file (SHA: test123)"
        ),
        SimpleNamespace(
            type="resource",
            resource=SimpleNamespace(
                text=resource_text,
                contents=[]
            )
        )
    ],
    structuredContent=None
)

output = extract_mcp_text(result)

print("Extracted content:")
print(output)

assert output == resource_text
assert "successfully downloaded" not in output

print("\nPASS: Embedded resource content extracted correctly.")
