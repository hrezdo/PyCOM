import network
from network import LoRa
import binascii
import time
import pycom
import socket
import struct
import json
import machine
from machine import Pin
import gc
import crypto
import math

VERSION = "v0.1 on 2017-03-21"

#SHT31 constants
SHT31_ADDR = 68
SHT31_STATUS_REGISTER = b'\xf3\x2d'
PERIODIC_MEASUREMENT_COMMAND = b'\x27\x37'

#Lora constants
_LORA_PKG_FORMAT = "!BBBBBBBBBBBBBBB"
OP_MODES = ["LoRa gateway", "Temperature & Humidity sensor", "Pulse counter"]
BANDWIDTH = ["125", "250"]
POWER_TX = ["2", "4", "6", "8", "10", "12", "14"]
CODING_RATE = ["4/5", "4/6", "4/7", "4/8"]
SPREADING_FACTOR = ["7", "8", "9", "10", "11", "12"]

pycom.heartbeat(False)
#averaging battery
numADCreadings = const(100)

i2c = machine.I2C(0, machine.I2C.MASTER, baudrate=100000, pins=('P8', 'P9'))
rtc = machine.RTC()

class LoRaNode():
	def __init__(self, i2c, rtc):

		self.now = 0
		self.lastReportTime = 0

		self.ID = machine.unique_id()
		self.IDhex = str(binascii.hexlify(machine.unique_id()))[2:-1]

		self.i2c = i2c
		self.i2cBuffer = bytearray(2)
		self.SHT31buffer = bytearray(6)
		#self.uart2 = uart2
		self.rtc = rtc
		self.currentTimeDate = self.rtc.now()

		self.lora = None
		self.lora_sock = None

		self.voltage = 100
		self.distance = 0

		self.wlan = network.WLAN()
		self.wlan.deinit()
		self.WiFiIsConnected = False
		self.WiFiIsConnectedLast = False

		self.config = None
		self.loadConfig()
		self.modeSwitch = Pin('P10', mode=Pin.IN, pull=Pin.PULL_UP)
		self.LED = Pin('P9', mode=Pin.OUT)

		self.trig = Pin('P21', mode=Pin.OUT, pull=None, alt=-1)
		self.echo = Pin('P22', mode=Pin.IN, pull=None, alt=-1)
		self.enable = Pin('P23', mode=Pin.OUT, pull=Pin.PULL_DOWN, alt=-1)
		self.enable.value(1)

	def blinkLED(self, times):
		for _ in range(times):
			self.LED.value(0)
			time.sleep(0.16)
			self.LED.value(1)
			time.sleep(0.16)

	def p(self, logItemSeverity, message):
		self.currentTimeDate = self.rtc.now()
		if (logItemSeverity == "ERROR" and self.config['serialVerbose'] > 0) or (logItemSeverity == "WARNING" and self.config['serialVerbose'] > 0) or (logItemSeverity == "INFO" and self.config['serialVerbose'] > 1) or ((logItemSeverity == "LORA" or logItemSeverity == "WAN"or logItemSeverity == "CoT") and self.config['serialVerbose'] > 2):
			print("[%s-%02d-%02d %02d:%02d:%02d] %s: %s" %(self.currentTimeDate[0],self.currentTimeDate[1],self.currentTimeDate[2],self.currentTimeDate[3],self.currentTimeDate[4],self.currentTimeDate[5],logItemSeverity,message))

	def loadConfig(self):
		try:
			configFile = open('/flash/nodeConfig.json', 'r')
			data = configFile.readall()
			self.config = json.loads(data)
			configFile.close()
			del data
			del configFile
			self.p("INFO", "Configuration loaded")
		except Exception as e:
			self.p("ERROR", "Loading of configuration failed")

	def saveConfig(self):
		configFile = open('/flash/nodeConfig.json', 'w')
		configFile.write(json.dumps(self.config))
		configFile.close()
		self.p("INFO", "Config updated")

	def rebootToConfigMode(self):
		self.config["bootToConfigMode"] = 1
		self.saveConfig()
		machine.reset()

	def run(self):
		self.p("INFO", "Device ID: %s" % (self.IDhex))
		swVersion = os.uname()
		self.p("INFO", "Firmware version: %s" % (swVersion[3]))
		del swVersion
		self.p("INFO", "Software version: %s" % (VERSION))

		if not self.modeSwitch.value():
			self.blinkLED(2)
			self.configMode()
		else:
			#self.blinkLED(1)
			self.parkingSensorMode()

	def measureDistance(self):
		self.trig.value(0)
		time.sleep_ms(2)
		self.trig.value(1)
		time.sleep_ms(10)
		self.trig.value(0)
		while not self.echo.value():
			pass
	  	t1 = time.ticks_us()
		while self.echo.value():
			pass
	  	t2 = time.ticks_us()
	  	distance = (t2 - t1) / 58.0
		return distance

	def sendUpdateToGateway(self):
		pkg = struct.pack(_LORA_PKG_FORMAT, self.config['sensorNr'], self.voltage, self.distance, self.remoteGatewayID[0], self.remoteGatewayID[1], self.remoteGatewayID[2], self.remoteGatewayID[3], self.remoteGatewayID[4], self.remoteGatewayID[5], self.ID[0], self.ID[1], self.ID[2], self.ID[3], self.ID[4], self.ID[5])

		iv = os.urandom(16) # crypto.getrandbits(128) returns 0s if WLAN or BT is not enabled
		cipher = crypto.AES(self.config["AESkey"], crypto.AES.MODE_CFB, iv)
		encryptedPkg = iv + cipher.encrypt(pkg)
		#print("voltage:%s" % (self.voltage))

		self.p("LORA", "raw: %s" % str(pkg))
		self.p("LORA", "encrypted: %s" % str(encryptedPkg))

		self.lora_sock.send(encryptedPkg)

	def parkingSensorMode(self):
		self.p("INFO", "Starting in Parking LoRa sensor mode (battery powered with deep sleep)")
		self.remoteGatewayID = binascii.unhexlify(self.config['remoteGatewayID'])

		try:
			self.lora = LoRa(mode=LoRa.LORA, frequency=self.config['frequency'], tx_power=int(self.config['powerTX']), bandwidth=self.config['bandwidth'], sf=self.config['spreadingFactor'], coding_rate=self.config['codingRate'], tx_iq=True)
			self.p("INFO", "LoRa radio interface initialized at %sHz with %sdBm TX power, %skHz bandwidth, spreading factor %s and coding rate %s" % (self.config['frequency'], self.config['powerTX'], BANDWIDTH[self.config['bandwidth']], self.config['spreadingFactor'], CODING_RATE[self.config['codingRate']-1]))
		except Exception as e:
			self.p("WARNING", "Error during Lora radio interface initialization: %s" % e)

		self.lora_sock = socket.socket(socket.AF_LORA, socket.SOCK_RAW)
		self.lora_sock.setblocking(False)

		time.sleep(1)
		self.distance = int(self.measureDistance())
		self.enable.value(0)
		if self.distance >= 598:
			self.p("WARNING", "Too far")
			self.distance = 255
		elif self.distance >= 250 and self.distance < 598:
			self.distance = 250
			self.p("INFO", "Distance 250cm or more")
		else:
			self.p("INFO", "Distance: %scm" % (self.distance))
			self.p("INFO", "Voltage: %sV" % (self.voltage))
		self.ADCloopMeanStdDev()
		self.sendUpdateToGateway()

		#time.sleep(5)
		machine.deepsleep(self.config['reportInterval']*1000)

	def configMode(self):
		self.p("INFO", "Starting in configuration mode")
		SSID = 'Sensor config ' + self.IDhex
		self.wlan.init(mode=network.WLAN.AP, ssid=SSID, auth=(network.WLAN.WPA2,'L0RaN0de'), channel=3, antenna=network.WLAN.INT_ANT)

	def ADCloopMeanStdDev():
	    adc = machine.ADC(0)
	    adcread = adc.channel(pin='P13')
	    samplesADC = [0.0]*numADCreadings; meanADC = 0.0
	    i = 0
	    while (i < numADCreadings):
	        adcint = adcread()
	        samplesADC[i] = adcint
	        meanADC += adcint
	        i += 1
	    meanADC /= numADCreadings
	    varianceADC = 0.0
	    for adcint in samplesADC:
	        varianceADC += (adcint - meanADC)**2
	    varianceADC /= (numADCreadings - 1)
	    print("%u ADC readings :\n%s" %(numADCreadings, str(samplesADC)))
	    print("Mean of ADC readings (0-1023) = %15.13f" % meanADC)
	    print("Mean of ADC readings (0-1000 mV) = %15.13f" % (meanADC*1000/1024))
	    print("Variance of ADC readings = %15.13f" % varianceADC)
	    print("10**6*Variance/(Mean**2) of ADC readings = %15.13f" % ((varianceADC*10**6)//(meanADC**2)))
	    print("Standard deviation of ADC readings = %15.13f" % math.sqrt(varianceADC))

node = LoRaNode(i2c, rtc)
node.run()
