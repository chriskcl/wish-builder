import { TextDecoder } from "node:util";

const UTF8_DECODER = new TextDecoder("utf-8", { fatal: true });
const NUMBER = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;

export class StrictJsonError extends Error {
  constructor(message) {
    super(message);
    this.name = "StrictJsonError";
  }
}

export function parseStrictJsonBytes(bytes) {
  if (!Buffer.isBuffer(bytes) && !(bytes instanceof Uint8Array)) {
    throw new StrictJsonError("JSON input must be bytes");
  }
  let text;
  try {
    text = UTF8_DECODER.decode(bytes);
  } catch {
    throw new StrictJsonError("JSON input must be valid UTF-8");
  }
  if (text.charCodeAt(0) === 0xfeff) {
    throw new StrictJsonError("JSON input must not start with a byte-order mark");
  }
  return new StrictJsonParser(text).parse();
}

class StrictJsonParser {
  constructor(text) {
    this.text = text;
    this.offset = 0;
  }

  parse() {
    this.skipWhitespace();
    if (this.offset === this.text.length) {
      this.fail("JSON input is empty");
    }
    const value = this.parseValue();
    this.skipWhitespace();
    if (this.offset !== this.text.length) {
      this.fail("JSON input must contain exactly one value");
    }
    return value;
  }

  parseValue() {
    const character = this.text[this.offset];
    if (character === "{") return this.parseObject();
    if (character === "[") return this.parseArray();
    if (character === '"') return this.parseString();
    if (character === "t") return this.parseLiteral("true", true);
    if (character === "f") return this.parseLiteral("false", false);
    if (character === "n") return this.parseLiteral("null", null);
    if (character === "-" || (character >= "0" && character <= "9")) {
      return this.parseNumber();
    }
    this.fail("JSON value is invalid");
  }

  parseObject() {
    this.offset += 1;
    const value = Object.create(null);
    const keys = new Set();
    this.skipWhitespace();
    if (this.consume("}")) return value;
    while (true) {
      if (this.text[this.offset] !== '"') {
        this.fail("JSON object keys must be strings");
      }
      const key = this.parseString();
      if (keys.has(key)) {
        this.fail(`JSON object contains duplicate key: ${JSON.stringify(key)}`);
      }
      keys.add(key);
      this.skipWhitespace();
      if (!this.consume(":")) this.fail("JSON object key must be followed by ':'");
      this.skipWhitespace();
      value[key] = this.parseValue();
      this.skipWhitespace();
      if (this.consume("}")) return value;
      if (!this.consume(",")) this.fail("JSON object entries must be separated by ','");
      this.skipWhitespace();
    }
  }

  parseArray() {
    this.offset += 1;
    const value = [];
    this.skipWhitespace();
    if (this.consume("]")) return value;
    while (true) {
      value.push(this.parseValue());
      this.skipWhitespace();
      if (this.consume("]")) return value;
      if (!this.consume(",")) this.fail("JSON array entries must be separated by ','");
      this.skipWhitespace();
    }
  }

  parseString() {
    this.offset += 1;
    let value = "";
    while (this.offset < this.text.length) {
      const character = this.text[this.offset++];
      if (character === '"') return value;
      if (character === "\\") {
        value += this.parseEscape();
        continue;
      }
      if (character.charCodeAt(0) < 0x20) {
        this.fail("JSON strings must escape control characters");
      }
      value += character;
    }
    this.fail("JSON string is not terminated");
  }

  parseEscape() {
    if (this.offset >= this.text.length) this.fail("JSON escape is incomplete");
    const escape = this.text[this.offset++];
    const simple = {
      '"': '"',
      "\\": "\\",
      "/": "/",
      b: "\b",
      f: "\f",
      n: "\n",
      r: "\r",
      t: "\t",
    };
    if (Object.hasOwn(simple, escape)) return simple[escape];
    if (escape !== "u") this.fail("JSON escape is invalid");
    const first = this.parseHexCodeUnit();
    if (first >= 0xd800 && first <= 0xdbff) {
      if (this.text.slice(this.offset, this.offset + 2) !== "\\u") {
        this.fail("JSON high surrogate must be followed by a low surrogate");
      }
      this.offset += 2;
      const second = this.parseHexCodeUnit();
      if (second < 0xdc00 || second > 0xdfff) {
        this.fail("JSON high surrogate must be followed by a low surrogate");
      }
      return String.fromCodePoint(0x10000 + ((first - 0xd800) << 10) + second - 0xdc00);
    }
    if (first >= 0xdc00 && first <= 0xdfff) {
      this.fail("JSON low surrogate must follow a high surrogate");
    }
    return String.fromCharCode(first);
  }

  parseHexCodeUnit() {
    const source = this.text.slice(this.offset, this.offset + 4);
    if (!/^[0-9A-Fa-f]{4}$/.test(source)) {
      this.fail("JSON unicode escape must contain four hexadecimal digits");
    }
    this.offset += 4;
    return Number.parseInt(source, 16);
  }

  parseLiteral(source, value) {
    if (this.text.slice(this.offset, this.offset + source.length) !== source) {
      this.fail("JSON literal is invalid");
    }
    this.offset += source.length;
    return value;
  }

  parseNumber() {
    NUMBER.lastIndex = this.offset;
    const match = NUMBER.exec(this.text);
    if (match === null) this.fail("JSON number is invalid");
    this.offset = NUMBER.lastIndex;
    const value = Number(match[0]);
    if (!Number.isFinite(value)) this.fail("JSON number must be finite");
    return value;
  }

  skipWhitespace() {
    while (
      this.text[this.offset] === " " ||
      this.text[this.offset] === "\t" ||
      this.text[this.offset] === "\r" ||
      this.text[this.offset] === "\n"
    ) {
      this.offset += 1;
    }
  }

  consume(character) {
    if (this.text[this.offset] !== character) return false;
    this.offset += 1;
    return true;
  }

  fail(message) {
    throw new StrictJsonError(`${message} at offset ${this.offset}`);
  }
}
